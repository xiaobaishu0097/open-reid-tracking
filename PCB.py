from __future__ import print_function, absolute_import
import argparse
import os

import numpy as np
import time
import datetime
import sys
import torch
from torch.backends import cudnn
import json

from reid import models
from reid.utils.my_utils import *
from reid.trainers import Trainer
from reid.evaluators import Evaluator
from reid.utils.logging import Logger
from reid.utils.serialization import save_checkpoint

'''
    ideas for better training from Dr. Yifan Sun
    
    train resnet BN by default                              check
    no crop                                                 check
    batch_size = 64 , lr = 0.1                              check
    dropout -- possible at layer: pool5                     check
    skip step-3 in RPP training                             check
    RPP classifier -- 2048 -> 256 -> 6 (average pooling)    check
'''


def main(args):
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    cudnn.benchmark = True
    # Redirect print to both console and log file
    date_str = '{}'.format(datetime.datetime.today().strftime('%Y-%m-%d_%H-%M-%S'))
    if (not args.evaluate) and args.log:
        sys.stdout = Logger(osp.join(args.logs_dir, 'log_{}.txt'.format(date_str)))
        # save opts
        with open(osp.join(args.logs_dir, 'args_{}.json'.format(date_str)), 'w') as fp:
            json.dump(vars(args), fp, indent=1)

    # Create data loaders
    dataset, num_classes, train_loader, query_loader, gallery_loader, camstyle_loader = \
        get_data(args.dataset, args.data_dir, args.height, args.width, args.batch_size, args.num_workers,
                 args.combine_trainval, args.crop, args.tracking_icams, args.tracking_fps, args.re, 0, args.camstyle)

    # Create model
    model = models.create('pcb', num_features=args.features, norm=args.norm,
                          dropout=args.dropout, num_classes=num_classes, last_stride=args.last_stride,
                          output_feature=args.output_feature)

    # Load from checkpoint
    start_epoch = best_top1 = 0
    if args.resume:
        if args.evaluate:
            model, start_epoch, best_top1 = checkpoint_loader(model, args.resume, eval_only=True)
        else:
            model, start_epoch, best_top1 = checkpoint_loader(model, args.resume)
        print("=> Start epoch {}  best top1 {:.1%}".format(start_epoch, best_top1))
    model = nn.DataParallel(model).cuda()

    # Evaluator
    evaluator = Evaluator(model)
    if args.evaluate:
        print("Test:")
        evaluator.evaluate(query_loader, gallery_loader, dataset.query, dataset.gallery, eval_only=True)
        return

    # Criterion
    criterion = nn.CrossEntropyLoss().cuda()

    if args.train:
        # Optimizer
        if hasattr(model.module, 'base'):  # low learning_rate the base network (aka. ResNet-50)
            base_param_ids = set(map(id, model.module.base.parameters()))
            new_params = [p for p in model.parameters() if id(p) not in base_param_ids]
            param_groups = [{'params': model.module.base.parameters(), 'lr_mult': 0.1},
                            {'params': new_params, 'lr_mult': 1.0}]
        else:
            param_groups = model.parameters()
        optimizer = torch.optim.SGD(param_groups, lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay,
                                    nesterov=True)

        # Trainer
        trainer = Trainer(model, criterion)

        # Schedule learning rate
        def adjust_lr(epoch):
            step_size = args.step_size
            lr = args.lr * (0.1 ** (epoch // step_size))
            for g in optimizer.param_groups:
                g['lr'] = lr * g.get('lr_mult', 1)

        # Draw Curve
        epoch_s = []
        loss_s = []
        prec_s = []

        # Start training
        for epoch in range(start_epoch, args.epochs):
            t0 = time.time()
            adjust_lr(epoch)
            # train_loss, train_prec = 0, 0
            train_loss, train_prec = trainer.train(epoch, train_loader, optimizer, fix_bn=args.fix_bn)

            if epoch < args.start_save:
                continue
            # skip evaluate
            top1 = 50

            is_best = top1 >= best_top1
            best_top1 = max(top1, best_top1)
            save_checkpoint({
                'state_dict': model.module.state_dict(),
                'epoch': epoch + 1,
                'best_top1': best_top1,
                'rpp': False,
            }, is_best, fpath=osp.join(args.logs_dir, 'checkpoint_{}.pth.tar'.format(date_str)))
            epoch_s.append(epoch)
            loss_s.append(train_loss)
            prec_s.append(train_prec)
            draw_curve(os.path.join(args.logs_dir, 'train_{}.jpg'.format(date_str)), epoch_s, loss_s, prec_s)

            t1 = time.time()
            t_epoch = t1 - t0
            print('\n * Finished epoch {:3d}  top1: {:5.1%}  best: {:5.1%}\n'.
                  format(epoch, top1, best_top1, ' *' if is_best else ''))
            print(
                '*************** Epoch takes time: {:^10.2f} *********************\n'.format(t_epoch))

        # Final test
        print('Test with best model:')
        model, start_epoch, best_top1 = checkpoint_loader(model, osp.join(args.logs_dir, 'model_best.pth.tar'),
                                                          eval_only=True)
        print("=> Start epoch {}  best top1 {:.1%}".format(start_epoch, best_top1))

        evaluator.evaluate(query_loader, gallery_loader, dataset.query, dataset.gallery, eval_only=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Softmax loss classification")
    parser.add_argument('--log', type=bool, default=1)
    # data
    parser.add_argument('-d', '--dataset', type=str, default='market1501', choices=datasets.names())
    parser.add_argument('-b', '--batch-size', type=int, default=64, help="batch size")
    parser.add_argument('-j', '--num-workers', type=int, default=4)
    parser.add_argument('--height', type=int, default=384, help="input height, default: 384 for PCB*")
    parser.add_argument('--width', type=int, default=128, help="input width, default: 128 for resnet*")
    parser.add_argument('--combine-trainval', action='store_true',
                        help="train and val sets together for training, val set alone for validation")
    parser.add_argument('--tracking_icams', type=int, default=0, help="specify if train on single iCam")
    parser.add_argument('--tracking_fps', type=int, default=1, help="specify if train on single iCam")
    parser.add_argument('--re', type=float, default=0, help="random erasing")
    # model
    parser.add_argument('--features', type=int, default=256)
    parser.add_argument('--dropout', type=float, default=0.5)
    parser.add_argument('-s', '--last_stride', type=int, default=1, choices=[1, 2])
    parser.add_argument('--output_feature', type=str, default='fc', choices=['pool5', 'fc'])
    parser.add_argument('--norm', action='store_true', help="normalize feat, default: False")
    # optimizer
    parser.add_argument('--lr', type=float, default=0.1,
                        help="learning rate of new parameters, for pretrained "
                             "parameters it is 10 times smaller than this")
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight-decay', type=float, default=5e-4)
    # training configs
    parser.add_argument('--train', action='store_true', help="train PCB model from start")
    parser.add_argument('--crop', action='store_true', help="resize then crop, default: False")
    parser.add_argument('--fix_bn', type=bool, default=0, help="fix (skip training) BN in base network")
    parser.add_argument('--resume', type=str, default='', metavar='PATH')
    parser.add_argument('--evaluate', action='store_true', help="evaluation only")
    parser.add_argument('--epochs', type=int, default=60)
    parser.add_argument('--step-size', type=int, default=40)
    parser.add_argument('--start_save', type=int, default=0, help="start saving checkpoints after specific epoch")
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--print-freq', type=int, default=1)
    # camstyle batchsize
    parser.add_argument('--camstyle', type=int, default=0)
    # misc
    working_dir = osp.dirname(osp.abspath(__file__))
    parser.add_argument('--data-dir', type=str, metavar='PATH', default=osp.join(working_dir, 'data'))
    parser.add_argument('--logs-dir', type=str, metavar='PATH', default=osp.join(working_dir, 'logs'))
    main(parser.parse_args())
