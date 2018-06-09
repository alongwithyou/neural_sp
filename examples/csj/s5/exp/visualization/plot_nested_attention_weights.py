#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Plot attention weights of the nested attention model (CSJ corpus)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from os.path import join, abspath, isdir
import sys
import argparse
import shutil

import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
plt.style.use('ggplot')
import seaborn as sns
sns.set_style("white")
blue = '#4682B4'
orange = '#D2691E'
green = '#006400'

# sns.set(font='IPAMincho')
sns.set(font='Noto Sans CJK JP')

sys.path.append(abspath('../../../'))
from models.load_model import load
from examples.csj.s5.exp.dataset.load_dataset_hierarchical import Dataset
from utils.directory import mkdir_join, mkdir
from utils.visualization.attention import plot_hierarchical_attention_weights, plot_nested_attention_weights
from utils.config import load_config

parser = argparse.ArgumentParser()
parser.add_argument('--data_save_path', type=str,
                    help='path to saved data')
parser.add_argument('--model_path', type=str,
                    help='path to the model to evaluate')
parser.add_argument('--epoch', type=int, default=-1,
                    help='the epoch to restore')
parser.add_argument('--eval_batch_size', type=int, default=1,
                    help='the size of mini-batch in evaluation')
parser.add_argument('--beam_width', type=int, default=1,
                    help='the size of beam in the main task')
parser.add_argument('--beam_width_sub', type=int, default=1,
                    help='the size of beam in the sub task')
parser.add_argument('--length_penalty', type=float, default=0,
                    help='length penalty in beam search decoding')
parser.add_argument('--coverage_penalty', type=float, default=0,
                    help='coverage penalty in beam search decoding')

from distutils.util import strtobool
parser.add_argument('--a2c_oracle', type=strtobool, default=False)

MAX_DECODE_LEN_WORD = 100
MIN_DECODE_LEN_WORD = 1
MAX_DECODE_LEN_CHAR = 200
MIN_DECODE_LEN_CHAR = 1


def main():

    args = parser.parse_args()

    # Load a config file (.yml)
    params = load_config(join(args.model_path, 'config.yml'), is_eval=True)

    # Load dataset
    dataset = Dataset(
        data_save_path=args.data_save_path,
        input_freq=params['input_freq'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        data_type='eval1',
        # data_type='eval2',
        # data_type='eval3',
        data_size=params['data_size'],
        label_type=params['label_type'], label_type_sub=params['label_type_sub'],
        batch_size=args.eval_batch_size,
        sort_utt=False, reverse=False, tool=params['tool'])
    params['num_classes'] = dataset.num_classes
    params['num_classes_sub'] = dataset.num_classes_sub

    # Load model
    model = load(model_type=params['model_type'],
                 params=params,
                 backend=params['backend'])

    # Restore the saved parameters
    model.load_checkpoint(save_path=args.model_path, epoch=args.epoch)

    # GPU setting
    model.set_cuda(deterministic=False, benchmark=True)

    save_path = mkdir_join(args.model_path, 'att_weights')

    ######################################################################

    # Clean directory
    if save_path is not None and isdir(save_path):
        shutil.rmtree(save_path)
        mkdir(save_path)

    for batch, is_new_epoch in dataset:
        if args.a2c_oracle:
            if dataset.is_test:
                max_label_num = 0
                for b in range(len(batch['xs'])):
                    if max_label_num < len(list(batch['ys_sub'][b])):
                        max_label_num = len(list(batch['ys_sub'][b]))

                ys_sub = []
                for b in range(len(batch['xs'])):
                    indices = dataset.char2idx(batch['ys_sub'][b])
                    ys_sub += [indices]
                    # NOTE: transcript is seperated by space('_')
            else:
                ys_sub = batch['ys_sub']
        else:
            ys_sub = None

        best_hyps, aw, best_hyps_sub, aw_sub, aw_dec, _ = model.decode(
            batch['xs'],
            beam_width=args.beam_width,
            max_decode_len=MAX_DECODE_LEN_WORD,
            min_decode_len=MIN_DECODE_LEN_WORD,
            beam_width_sub=args.beam_width_sub,
            max_decode_len_sub=MAX_DECODE_LEN_CHAR,
            min_decode_len_sub=MIN_DECODE_LEN_CHAR,
            length_penalty=args.length_penalty,
            coverage_penalty=args.coverage_penalty,
            teacher_forcing=args.a2c_oracle,
            ys_sub=ys_sub)

        for b in range(len(batch['xs'])):
            word_list = dataset.idx2word(best_hyps[b], return_list=True)
            if dataset.label_type_sub == 'word':
                char_list = dataset.idx2word(
                    best_hyps_sub[b], return_list=True)
            else:
                char_list = dataset.idx2char(
                    best_hyps_sub[b], return_list=True)

            speaker = batch['input_names'][b].split('_')[0]

            # word to acoustic & character to acoustic
            plot_hierarchical_attention_weights(
                aw[b][:len(word_list)],  # TODO: fix this
                aw_sub[b][:len(char_list)],
                label_list=word_list,
                label_list_sub=char_list,
                spectrogram=batch['xs'][b][:, :dataset.input_freq],
                save_path=mkdir_join(save_path, speaker,
                                     batch['input_names'][b] + '.png'),
                figsize=(40, 8)
            )

            # word to characater
            plot_nested_attention_weights(
                aw_dec[b][:len(word_list), :len(char_list)],
                label_list=word_list,
                label_list_sub=char_list,
                save_path=mkdir_join(save_path, speaker,
                                     batch['input_names'][b] + '_word2char.png'),
                figsize=(40, 8)
            )

            # with open(join(save_path, speaker, batch['input_names'][b] + '.txt'), 'w') as f:
            #     f.write(batch['ys'][b])

        if is_new_epoch:
            break


if __name__ == '__main__':
    main()
