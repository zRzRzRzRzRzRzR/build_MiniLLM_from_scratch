#! -*- coding: utf-8 -*-
'''
预训练
启动命令: nohup torchrun --standalone --nproc_per_node=4 pretrain.py --name baby > nohup.log&
'''
import torch.nn as nn
import torch
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset
from torch.utils.data.distributed import DistributedSampler
from bert4torch.models import build_transformer_model, BaseModel, BaseModelDDP
from bert4torch.snippets import ListDataset, DottableDict, log_info, get_weight_decay_optim_groups
from bert4torch.callbacks import Checkpoint, Logger, EarlyStopping, Tensorboard, Evaluator
from bert4torch.optimizers import get_linear_schedule_with_warmup
from glob import glob
import os
import numpy as np
import inspect


# 基本参数
args = DottableDict()
args.compile = False
args.ddp_config = BaseModelDDP.init_process_group() if int(os.environ.get("RANK", -1)) != -1 else None
args.lr = 3e-4
args.batch_size = 32
args.grad_accumulation_steps = 1
args.pad_token_id = 0
args.max_length = 1024
args.epochs = 1
args.weight_decay = 0.1
args.interval = 2000
args.data_path = '/home/hfai/data/pretrain/pretrain_data_bin/**/*.bin'
args.device = 'cuda' if torch.cuda.is_available() else 'cpu'
args.config_path = '../config/bert4torch_config.json'
args.resume_path = '/home/hfai/h01305/projects/build_llm_from_scratch/ckpt/L12_H1024_A8-WithWudao/96000_3.2223'  # None

if False:
    # 不含悟道语料
    args.save_dir = '/home/hfai/h01305/projects/build_llm_from_scratch/ckpt/L12_H1024_A8-NoWudao'
    args.filenames = [i for i in glob(args.data_path, recursive=True) if 'wudaocorpus' not in i]
else:
    # 含悟道语料
    args.save_dir = '/home/hfai/h01305/projects/build_llm_from_scratch/ckpt/L12_H1024_A8-WithWudao'
    args.filenames = [i for i in glob(args.data_path, recursive=True)]

# ========================加载数据集========================
class MyDataset(Dataset):
    def __init__(self, filenames):
        """加载数据"""
        self.data = []
        self.index_map = {}
        self.token_size, self.smp_size = 0, 0
        for fi, filename in enumerate(filenames):
            with open(filename,'r') as f:
                nbytes = f.seek(0,2)
                flen = f.tell() // np.dtype('uint16').itemsize
            self.token_size += flen
            self.index_map.update({self.smp_size+i:(fi, i) for i in range(flen//args.max_length)})
            self.smp_size += flen//args.max_length
            self.data.append(np.memmap(filename, dtype=np.dtype('uint16'), shape=(flen//args.max_length, args.max_length)))
        log_info(f'token_size: {self.token_size}, smp_size: {self.smp_size}')

    def __len__(self):
        return self.smp_size
    
    def __getitem__(self, index: int):
        fi, i = self.index_map[index]
        sample = self.data[fi][i]
        X = np.array(sample[:-1]).astype(np.int64)
        Y = np.array(sample[1:]).astype(np.int64)
        return torch.from_numpy(X), torch.from_numpy(Y)

dataset = MyDataset(args.filenames)
train_dataloader = DataLoader(dataset, batch_size=args.batch_size, pin_memory=False, 
                              drop_last=False, shuffle=False, num_workers=0 if os.name == 'nt' else 4,
                              sampler=DistributedSampler(dataset) if args.ddp_config is not None else None) 

model = build_transformer_model(config_path=args.config_path, checkpoint_path=None, add_trainer=True)
model.to(args.device)

if args.compile:
    print("compiling the model... (takes a ~minute)")
    model = torch.compile(model)

if args.ddp_config is not None:
    model = BaseModelDDP(model, master_rank=0, device_ids=[args.ddp_config.local_rank], output_device=args.ddp_config.local_rank, find_unused_parameters=False)
model.print_trainable_parameters()

class CrossEntropyLoss(nn.CrossEntropyLoss):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
    def forward(self, logits, labels):
        '''
        logits: [btz, seq_len, vocab_size]
        labels: token_ids: [btz, seq_len]
        '''
        raw_dtyps = logits.dtype
        logits = logits.to(torch.float32)        
        logits = logits.reshape(-1, logits.shape[-1])
        labels = labels.flatten()
        loss = super().forward(logits, labels)

        return loss.to(raw_dtyps)

# 创建optimizer
optim_groups = get_weight_decay_optim_groups(model, weight_decay=args.weight_decay)
use_fused = 'fused' in inspect.signature(torch.optim.AdamW).parameters
extra_args = dict(fused=True) if use_fused else dict()
optimizer = optim.AdamW(optim_groups, lr=args.lr, betas=(0.9, 0.95), **extra_args)

scheduler = get_linear_schedule_with_warmup(optimizer, 5000, len(train_dataloader)*args.epochs)
model.compile(loss=CrossEntropyLoss(ignore_index=args.pad_token_id), optimizer=optimizer, scheduler=scheduler, 
              grad_accumulation_steps=args.grad_accumulation_steps, clip_grad_norm=1.0, mixed_precision=True)

if args.resume_path:
    model.resume_from_checkpoint(args.resume_path, mapping=lambda x: x.replace('module.', ''))


if __name__ == '__main__':
    logger = Logger(args.save_dir+'/log_pretrain.log')
    checkpoint = Checkpoint(monitor='loss', epoch_or_step='step', min_max='min', verbose=0, interval=args.interval, 
                            save_dir=args.save_dir+'/{step}_{loss:.4f}', max_save_count=5, save_on_train_end=True)
    ts_board = Tensorboard(args.save_dir+'/tensorboard')  # tensorboard
    callbacks=[checkpoint, logger, ts_board]
    if args.ddp_config is not None:
        model.disable_run_callbacks(callbacks)

    model.fit(train_dataloader, steps_per_epoch=None, epochs=args.epochs, callbacks=callbacks)
else:
    model.load_weights('./best_model_pretain.pt', strict=False)