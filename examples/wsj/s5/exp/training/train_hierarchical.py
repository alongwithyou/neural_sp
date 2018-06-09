#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Train the hierarchical model (WSJ corpus)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import os
import sys
import time
from setproctitle import setproctitle
import copy
import argparse
from tensorboardX import SummaryWriter
from tqdm import tqdm
import cProfile

torch.manual_seed(1623)
torch.cuda.manual_seed_all(1623)

sys.path.append(os.path.abspath('../../../'))
from models.load_model import load
from examples.wsj.s5.exp.dataset.load_dataset_hierarchical import Dataset
from examples.wsj.s5.exp.metrics.character import eval_char
from examples.wsj.s5.exp.metrics.word import eval_word
from utils.training.learning_rate_controller import Controller
from utils.training.plot import plot_loss
from utils.training.updater_hierarchical import Updater
from utils.training.logging import set_logger
from utils.directory import mkdir_join
from utils.config import load_config, save_config

parser = argparse.ArgumentParser()
parser.add_argument('--gpu', type=int, default=-1,
                    help='the index of GPU (negative value indicates CPU)')
parser.add_argument('--config_path', type=str, default=None,
                    help='path to the configuration file')
parser.add_argument('--model_save_path', type=str,
                    help='path to save the model')
parser.add_argument('--saved_model_path', type=str, default=None,
                    help='path to the saved model to retrain')
parser.add_argument('--data_save_path', type=str, help='path to saved data')

MAX_DECODE_LEN_WORD = 32
MAX_DECODE_LEN_CHAR = 199


def main():

    args = parser.parse_args()

    ##################################################
    # DATSET
    ##################################################
    if args.model_save_path is not None:
        # Load a config file (.yml)
        params = load_config(args.config_path)
    # NOTE: Retrain the saved model from the last checkpoint
    elif args.saved_model_path is not None:
        params = load_config(os.path.join(args.saved_model_path, 'config.yml'))
    else:
        raise ValueError("Set model_save_path or saved_model_path.")

    # Load dataset
    train_data = Dataset(
        data_save_path=args.data_save_path,
        input_freq=params['input_freq'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        data_type=params['data_size'], data_size=params['data_size'],
        label_type=params['label_type'],
        label_type_sub=params['label_type_sub'],
        batch_size=params['batch_size'], max_epoch=params['num_epoch'],
        max_frame_num=params['max_frame_num'],
        min_frame_num=params['min_frame_num'],
        sort_utt=True, sort_stop_epoch=params['sort_stop_epoch'],
        tool=params['tool'], num_enque=None,
        dynamic_batching=params['dynamic_batching'])
    dev_data = Dataset(
        data_save_path=args.data_save_path,
        input_freq=params['input_freq'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        data_type='test_dev93', data_size=params['data_size'],
        label_type=params['label_type'],
        label_type_sub=params['label_type_sub'],
        batch_size=params['batch_size'],
        shuffle=True, tool=params['tool'])
    test_data = Dataset(
        data_save_path=args.data_save_path,
        input_freq=params['input_freq'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        data_type='test_eval92', data_size=params['data_size'],
        label_type=params['label_type'],
        label_type_sub=params['label_type_sub'],
        batch_size=1, tool=params['tool'])
    params['num_classes'] = train_data.num_classes
    params['num_classes_sub'] = train_data.num_classes_sub

    ##################################################
    # MODEL
    ##################################################
    # Model setting
    model = load(model_type=params['model_type'],
                 params=params,
                 backend=params['backend'])

    if args.model_save_path is not None:
        # Set save path
        save_path = mkdir_join(
            args.model_save_path, params['backend'],
            params['model_type'],
            params['label_type'] + '_' + params['label_type_sub'],
            params['data_size'], model.name)
        model.set_save_path(save_path)

        # Save config file
        save_config(config_path=args.config_path, save_path=model.save_path)

        # Setting for logging
        logger = set_logger(model.save_path)

        if os.path.isdir(params['char_init']):
            # NOTE: Start training from the pre-trained character model
            model.load_checkpoint(
                save_path=params['char_init'], epoch=-1,
                load_pretrained_model=True)

        # Count total parameters
        for name in sorted(list(model.num_params_dict.keys())):
            num_params = model.num_params_dict[name]
            logger.info("%s %d" % (name, num_params))
        logger.info("Total %.3f M parameters" %
                    (model.total_parameters / 1000000))

        # Define optimizer
        model.set_optimizer(
            optimizer=params['optimizer'],
            learning_rate_init=float(params['learning_rate']),
            weight_decay=float(params['weight_decay']),
            clip_grad_norm=params['clip_grad_norm'],
            lr_schedule=False,
            factor=params['decay_rate'],
            patience_epoch=params['decay_patient_epoch'])

        epoch, step = 1, 0
        learning_rate = float(params['learning_rate'])
        metric_dev_best = 1

    # NOTE: Retrain the saved model from the last checkpoint
    elif args.saved_model_path is not None:
        # Set save path
        model.save_path = args.saved_model_path

        # Setting for logging
        logger = set_logger(model.save_path)

        # Define optimizer
        model.set_optimizer(
            optimizer=params['optimizer'],
            learning_rate_init=float(params['learning_rate']),  # on-the-fly
            weight_decay=float(params['weight_decay']),
            clip_grad_norm=params['clip_grad_norm'],
            lr_schedule=False,
            factor=params['decay_rate'],
            patience_epoch=params['decay_patient_epoch'])

        # Restore the last saved model
        epoch, step, learning_rate, metric_dev_best = model.load_checkpoint(
            save_path=args.saved_model_path, epoch=-1, restart=True)

    else:
        raise ValueError("Set model_save_path or saved_model_path.")

    train_data.epoch = epoch - 1

    # GPU setting
    model.set_cuda(deterministic=False, benchmark=True)

    logger.info('PID: %s' % os.getpid())
    logger.info('USERNAME: %s' % os.uname()[1])

    # Set process name
    setproctitle('wsj_' + params['backend'] + '_' + params['model_type'] + '_' +
                 params['label_type'] + '_' + params['label_type_sub'] + '_' + params['data_size'])

    # Define learning rate controller
    lr_controller = Controller(
        learning_rate_init=learning_rate,
        backend=params['backend'],
        decay_type=params['decay_type'],
        decay_start_epoch=params['decay_start_epoch'],
        decay_rate=params['decay_rate'],
        decay_patient_epoch=params['decay_patient_epoch'],
        lower_better=True)

    # Setting for tensorboard
    if params['backend'] == 'pytorch':
        tf_writer = SummaryWriter(model.save_path)

    # Set the updater
    update = Updater(params['clip_grad_norm'], params['backend'])

    ##################################################
    # TRAINING LOOP
    ##################################################
    csv_steps, csv_loss_train, csv_loss_dev = [], [], []
    start_time_train = time.time()
    start_time_epoch = time.time()
    start_time_step = time.time()
    not_improved_epoch = 0
    best_model = model
    loss_train_mean, loss_main_train_mean, loss_sub_train_mean = 0., 0., 0.
    pbar_epoch = tqdm(total=len(train_data))
    while True:
        # Compute loss in the training set (including parameter update)
        batch_train, is_new_epoch = train_data.next()
        model, loss_train, loss_main_train, loss_sub_train = update(
            model, batch_train)
        loss_train_mean += loss_train
        loss_main_train_mean += loss_main_train
        loss_sub_train_mean += loss_sub_train
        pbar_epoch.update(len(batch_train['xs']))

        if (step + 1) % params['print_step'] == 0:
            # Compute loss in the dev set
            batch_dev = dev_data.next()[0]
            model, loss_dev, loss_main_dev, loss_sub_dev = update(
                model, batch_dev, is_eval=True)

            loss_train_mean /= params['print_step']
            loss_main_train_mean /= params['print_step']
            loss_sub_train_mean /= params['print_step']
            csv_steps.append(step)
            csv_loss_train.append(loss_train_mean)
            csv_loss_dev.append(loss_dev)

            # Logging by tensorboard
            if params['backend'] == 'pytorch':
                tf_writer.add_scalar('train/loss', loss_train_mean, step + 1)
                tf_writer.add_scalar(
                    'train/loss_main', loss_main_train_mean, step + 1)
                tf_writer.add_scalar(
                    'train/loss_sub', loss_sub_train_mean, step + 1)
                tf_writer.add_scalar('dev/loss', loss_dev, step + 1)
                tf_writer.add_scalar('dev/loss_main', loss_main_dev, step + 1)
                tf_writer.add_scalar('dev/loss_sub', loss_sub_dev, step + 1)
                # for name, param in model.named_parameters():
                #     name = name.replace('.', '/')
                #     tf_writer.add_histogram(
                #         name, param.data.cpu().numpy(), step + 1)
                #     if param.grad is not None:
                #         tf_writer.add_histogram(
                #             name + '/grad', param.grad.data.cpu().numpy(), step + 1)
                #     # TODO: fix this

            duration_step = time.time() - start_time_step
            logger.info("...Step:%d(epoch:%.3f) loss:%.3f/%.3f/%.3f(%.3f/%.3f/%.3f)/lr:%.5f/batch:%d/x_lens:%d (%.3f min)" %
                        (step + 1, train_data.epoch_detail,
                         loss_train_mean, loss_main_train_mean, loss_sub_train_mean,
                         loss_dev, loss_main_dev, loss_sub_dev,
                         learning_rate, train_data.current_batch_size,
                         max(len(x)
                             for x in batch_train['xs']) * params['num_stack'],
                         duration_step / 60))
            start_time_step = time.time()
            loss_train_mean, loss_main_train_mean, loss_sub_train_mean = 0., 0., 0.
        step += 1

        # Save checkpoint and evaluate model per epoch
        if is_new_epoch:
            duration_epoch = time.time() - start_time_epoch
            logger.info('===== EPOCH:%d (%.3f min) =====' %
                        (epoch, duration_epoch / 60))

            # Save fugure of loss
            plot_loss(csv_loss_train, csv_loss_dev, csv_steps,
                      save_path=model.save_path)

            if epoch < params['eval_start_epoch']:
                # Save the model
                model.save_checkpoint(model.save_path, epoch, step,
                                      learning_rate, metric_dev_best)
            else:
                start_time_eval = time.time()
                # dev
                if model.main_loss_weight > 0:
                    metric_dev, _ = eval_word(
                        models=[model],
                        dataset=dev_data,
                        eval_batch_size=1,
                        beam_width=1,
                        max_decode_len=MAX_DECODE_LEN_WORD,
                        max_decode_len_sub=MAX_DECODE_LEN_CHAR)
                    logger.info('  WER (dev93, main): %.3f %%' %
                                (metric_dev * 100))
                else:
                    wer_dev_sub, metric_dev, _ = eval_char(
                        models=[model],
                        dataset=dev_data,
                        eval_batch_size=1,
                        beam_width=1,
                        max_decode_len=MAX_DECODE_LEN_CHAR)
                    logger.info('  WER / CER (dev93, sub): %.3f / %.3f %%' %
                                ((wer_dev_sub * 100), (metric_dev * 100)))

                if metric_dev < metric_dev_best:
                    metric_dev_best = metric_dev
                    not_improved_epoch = 0
                    best_model = copy.deepcopy(model)
                    logger.info('||||| Best Score |||||')

                    # Save the model
                    model.save_checkpoint(model.save_path, epoch, step,
                                          learning_rate, metric_dev_best)

                    # test
                    if model.main_loss_weight > 0:
                        wer_eval92, _ = eval_word(
                            models=[model],
                            dataset=test_data,
                            eval_batch_size=1,
                            beam_width=1,
                            max_decode_len=MAX_DECODE_LEN_WORD,
                            max_decode_len_sub=MAX_DECODE_LEN_CHAR)
                        logger.info('  WER (eval92, main): %.3f %%' %
                                    (wer_eval92 * 100))
                    else:
                        wer_eval92_sub, cer_eval92_sub, _ = eval_char(
                            models=[model],
                            dataset=test_data,
                            eval_batch_size=1,
                            beam_width=1,
                            max_decode_len=MAX_DECODE_LEN_CHAR)
                        logger.info(' WER / CER (eval92, sub): %.3f / %.3f %%' %
                                    ((wer_eval92_sub * 100), (cer_eval92_sub * 100)))
                else:
                    not_improved_epoch += 1

                duration_eval = time.time() - start_time_eval
                logger.info('Evaluation time: %.3f min' % (duration_eval / 60))

                # Early stopping
                if not_improved_epoch == params['not_improved_patient_epoch']:
                    break

                # Update learning rate
                model.optimizer, learning_rate = lr_controller.decay_lr(
                    optimizer=model.optimizer,
                    learning_rate=learning_rate,
                    epoch=epoch,
                    value=metric_dev)

                if epoch == params['convert_to_sgd_epoch']:
                    # Convert to fine-tuning stage
                    model.set_optimizer(
                        'sgd',
                        learning_rate_init=learning_rate,
                        weight_decay=float(params['weight_decay']),
                        clip_grad_norm=params['clip_grad_norm'],
                        lr_schedule=False,
                        factor=params['decay_rate'],
                        patience_epoch=params['decay_patient_epoch'])
                    logger.info('========== Convert to SGD ==========')

                    # Inject Gaussian noise to all parameters
                    if float(params['weight_noise_std']) > 0:
                        model.weight_noise_injection = True

            pbar_epoch = tqdm(total=len(train_data))

            if epoch == params['num_epoch']:
                break

            start_time_step = time.time()
            start_time_epoch = time.time()
            epoch += 1

    duration_train = time.time() - start_time_train
    logger.info('Total time: %.3f hour' % (duration_train / 3600))

    if params['backend'] == 'pytorch':
        tf_writer.close()

    # TODO: evaluate the best model by beam search here
    if best_model is not None:
        pass

    # Training was finished correctly
    with open(os.path.join(model.save_path, 'COMPLETE'), 'w') as f:
        f.write('')

    return model.save_path


if __name__ == '__main__':
    # Setting for profiling
    pr = cProfile.Profile()
    save_path = pr.runcall(main)
    pr.dump_stats(os.path.join(save_path, 'train.profile'))
