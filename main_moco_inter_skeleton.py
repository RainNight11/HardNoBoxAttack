#!/usr/bin/env python
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import argparse
import builtins
import math
import os
import random
import shutil
import sys
import time
import warnings

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data


import moco.builder_inter
from torch.utils.tensorboard import SummaryWriter
from dataset import get_pretraining_set_inter


parser = argparse.ArgumentParser(description='Training')
parser.add_argument('-j', '--workers', default=2, type=int, metavar='N',
                    help='number of data loading workers (default: 32)')
parser.add_argument('--epochs', default=451, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=64, type=int,
                    metavar='N',
                    help='mini-batch size')
parser.add_argument('--lr', '--learning-rate', default=0.01, type=float,
                    metavar='LR', help='initial learning rate', dest='lr')
parser.add_argument('--schedule', default=[160, 351], nargs='*', type=int,
                    help='learning rate schedule (when to drop lr by 10x)')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum of SGD solver')
parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)',
                    dest='weight_decay')
parser.add_argument('-p', '--print-freq', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--seed', default=None, type=int,
                    help='seed for initializing training. ')
parser.add_argument('--gpu', default=0, type=int,
                    help='GPU id to use.')
parser.add_argument('--checkpoint-path', default='./checkpoints', type=str)
parser.add_argument('--skeleton-representation',default='seq-based_and_graph-based', type=str,
                    help='pair of skeleton-representations to use for self supervised training')
parser.add_argument('--pre-dataset', default='ntu60', type=str,
                    help='which dataset to use for self supervised training (ntu60 or ntu120)')
parser.add_argument('--protocol', default='cross_view', type=str,
                    help='training protocol cross_view/cross_subject/cross_setup')

# moco specific configs:
parser.add_argument('--moco-dim', default=128, type=int,
                    help='feature dimension (default: 128)')
parser.add_argument('--moco-k', default=16384, type=int,
                    help='queue size; number of negative keys (default: 16384)')
parser.add_argument('--moco-m', default=0.999, type=float,
                    help='moco momentum of updating key encoder (default: 0.999)')
parser.add_argument('--moco-t', default=0.07, type=float,
                    help='softmax temperature (default: 0.07)')
parser.add_argument('--mlp', action='store_true',
                    help='use mlp head')
parser.add_argument('--cos', action='store_true',
                    help='use cosine lr schedule')


def main():
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')

    ngpus_per_node = torch.cuda.device_count()
    # Simply call main_worker function
    main_worker(0, ngpus_per_node, args)

def main_worker(gpu, ngpus_per_node, args):
    args.gpu = gpu


    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))

    # training dataset
    from options import options_pretraining as options 
    if args.pre_dataset == 'ntu60' and args.protocol == 'cross_view':
        opts = options.opts_ntu_60_cross_view()
    elif args.pre_dataset == 'ntu60' and args.protocol == 'cross_subject':
        opts = options.opts_ntu_60_cross_subject()


    opts.train_feeder_args['input_representations'] = args.skeleton_representation

    # create model

    model = moco.builder_inter.MoCo(args.skeleton_representation,opts.bi_gru_model_args,opts.agcn_model_args,opts.hcn_model_args,args.moco_dim,args.moco_k,args.moco_m,args.moco_t,args.mlp)


    print("options",opts.train_feeder_args)

    if args.gpu is not None:

        model = model.cuda()


    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda(args.gpu)

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            if args.gpu is None:
                checkpoint = torch.load(args.resume)
            else:
                # Map model to be loaded to specified single gpu.
                loc = 'cuda:{}'.format(args.gpu)
                #checkpoint = torch.load(args.resume, map_location=loc)
                checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
            del checkpoint
            torch.cuda.empty_cache()
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    ## Data loading code
    train_dataset = get_pretraining_set_inter(opts)


    train_sampler = None

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True, sampler=train_sampler, drop_last=True)

    writer = SummaryWriter(args.checkpoint_path)

    for epoch in range(args.start_epoch, args.epochs):

        adjust_learning_rate(optimizer, epoch, args)

        # train for one epoch
        loss,acc1,acc2= train(train_loader, model, criterion, optimizer, epoch, args)
        writer.add_scalar('train_loss', loss.avg, global_step=epoch)
        writer.add_scalar('acc_s1',acc1.avg, global_step=epoch)
        writer.add_scalar('acc_s2',acc2.avg, global_step=epoch)
        if epoch % 10 == 0:
                    # print('new:', model.state_dict())
                  save_checkpoint({
                      'epoch': epoch + 1,
                      'state_dict': model.state_dict(),
                      'optimizer': optimizer.state_dict(),
                  }, is_best=False, filename=args.checkpoint_path+'/checkpoint_{:04d}.pth.tar'.format(epoch))


def train(train_loader, model, criterion, optimizer, epoch, args):
    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    s1_top1 = AverageMeter('S1_Acc@1', ':6.2f')
    s2_top1 = AverageMeter('S2_Acc@1', ':6.2f')
    progress = ProgressMeter(
        len(train_loader),
        [batch_time, losses, s1_top1, s2_top1],
        prefix="Epoch: [{}] Lr_rate [{}]".format(epoch,optimizer.param_groups[0]['lr']))

    # switch to train mode
    model.train()

    end = time.time()
    for i, (input_s1_v1, input_s2_v1,input_s1_v2,input_s2_v2) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        inputs= [input_s1_v1, input_s2_v1,input_s1_v2,input_s2_v2]
        if args.gpu is not None:
            inputs[0] =inputs[0].float().cuda(args.gpu, non_blocking=True)
            inputs[1] =inputs[1].float().cuda(args.gpu, non_blocking=True)
            inputs[2] =inputs[2].float().cuda(args.gpu, non_blocking=True)
            inputs[3] =inputs[3].float().cuda(args.gpu, non_blocking=True)



        # compute output
        output, target = model(inputs[0],inputs[1],inputs[2],inputs[3])

        loss_1 = criterion(output[0], target[0])
        loss_2 = criterion(output[1], target[1])
        loss = loss_1 + loss_2


        batch_size = output[0].size(0)
        losses.update(loss.item(), batch_size)

        # measure accuracy of model s1 and s2 individually
        # acc1/acc5 are (K+1)-way contrast classifier accuracy
        # measure accuracy and record loss
        s1_acc1, _ = accuracy(output[0], target[0], topk=(1, 5))
        s2_acc1, _ = accuracy(output[1], target[1], topk=(1, 5))
        s1_top1.update(s1_acc1[0], batch_size)
        s2_top1.update(s2_acc1[0], batch_size)



        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.display(i)

    return losses, s1_top1, s2_top1

def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'model_best_val.pth.tar')


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self, name, fmt=':f'):
        self.name = name
        self.fmt = fmt
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = '{name} {val' + self.fmt + '} ({avg' + self.fmt + '})'
        return fmtstr.format(**self.__dict__)


class ProgressMeter(object):
    def __init__(self, num_batches, meters, prefix=""):
        self.batch_fmtstr = self._get_batch_fmtstr(num_batches)
        self.meters = meters
        self.prefix = prefix

    def display(self, batch):
        entries = [self.prefix + self.batch_fmtstr.format(batch)]
        entries += [str(meter) for meter in self.meters]
        print('\t'.join(entries))

    def _get_batch_fmtstr(self, num_batches):
        num_digits = len(str(num_batches // 1))
        fmt = '{:' + str(num_digits) + 'd}'
        return '[' + fmt + '/' + fmt.format(num_batches) + ']'


def adjust_learning_rate(optimizer, epoch, args):
    """Decay the learning rate based on schedule"""
    lr = args.lr
    if args.cos:  # cosine lr schedule
        lr *= 0.5 * (1. + math.cos(math.pi * epoch / args.epochs))
    else:  # stepwise lr schedule
        for milestone in args.schedule:
            lr *= 0.1 if epoch >= milestone else 1.
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


if __name__ == '__main__':
    main()
