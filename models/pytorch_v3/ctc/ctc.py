#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""The Connectionist Temporal Classification model (pytorch)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

try:
    import warpctc_pytorch
except:
    raise ImportError('Install warpctc_pytorch.')

import numpy as np
import copy
import torch
from torch.autograd import Variable
import torch.nn.functional as F
from torch.nn.modules.loss import _assert_no_grad

from models.pytorch_v3.base import ModelBase
from models.pytorch_v3.linear import LinearND
from models.pytorch_v3.encoders.load_encoder import load
from models.pytorch_v3.criterion import cross_entropy_label_smoothing
from models.pytorch_v3.ctc.decoders.greedy_decoder import GreedyDecoder
from models.pytorch_v3.ctc.decoders.beam_search_decoder import BeamSearchDecoder
# from models.pytorch_v3.ctc.decoders.beam_search_decoder2 import BeamSearchDecoder
from models.pytorch_v3.utils import np2var, var2np, pad_list
from utils.io.inputs.frame_stacking import stack_frame
from utils.io.inputs.splicing import do_splice


class _CTC(warpctc_pytorch._CTC):
    @staticmethod
    def forward(ctx, acts, labels, act_lens, label_lens, size_average=False):
        is_cuda = True if acts.is_cuda else False
        acts = acts.contiguous()
        loss_func = warpctc_pytorch.gpu_ctc if is_cuda else warpctc_pytorch.cpu_ctc
        grads = torch.zeros(acts.size()).type_as(acts)
        minibatch_size = acts.size(1)
        costs = torch.zeros(minibatch_size).cpu()
        loss_func(acts,
                  grads,
                  labels,
                  label_lens,
                  act_lens,
                  minibatch_size,
                  costs)
        if size_average:
            # Compute the avg. log-probability per frame and batch sample.
            costs = torch.FloatTensor([costs.mean()])
        else:
            costs = torch.FloatTensor([costs.sum()])
        ctx.grads = Variable(grads)
        return costs


def my_warpctc(acts, labels, act_lens, label_lens, size_average=False):
    """Chainer like CTC Loss
    acts: Tensor of (seqLength x batch x outputDim) containing output from network
    labels: 1 dimensional Tensor containing all the targets of the batch in one sequence
    act_lens: Tensor of size (batch) containing size of each output sequence from the network
    act_lens: Tensor of (batch) containing label length of each example
    """
    assert len(labels.size()) == 1  # labels must be 1 dimensional
    _assert_no_grad(labels)
    _assert_no_grad(act_lens)
    _assert_no_grad(label_lens)
    return _CTC.apply(acts, labels, act_lens, label_lens, size_average)


warpctc = warpctc_pytorch.CTCLoss()


class CTC(ModelBase):
    """The Connectionist Temporal Classification model.
    Args:
        input_size (int): the dimension of input features
        encoder_type (string): the type of the encoder. Set lstm or gru or rnn.
        encoder_bidirectional (bool): if True create a bidirectional encoder
        encoder_num_units (int): the number of units in each layer
        encoder_num_proj (int): the number of nodes in recurrent projection layer
        encoder_num_layers (int): the number of layers of the encoder
        fc_list (list):
        dropout_input (float): the probability to drop nodes in input-hidden connection
        dropout_encoder (float): the probability to drop nodes in hidden-hidden connection
        num_classes (int): the number of classes of target labels
            (excluding the blank class)
        parameter_init_distribution (string): uniform or normal or orthogonal
            or constant distribution
        parameter_init (float): Range of uniform distribution to initialize
            weight parameters
        recurrent_weight_orthogonal (bool): if True, recurrent weights are
            orthogonalized
        init_forget_gate_bias_with_one (bool): if True, initialize the forget
            gate bias with 1
        subsample_list (list): subsample in the corresponding layers (True)
            ex.) [False, True, True, False] means that subsample is conducted
                in the 2nd and 3rd layers.
        subsample_type (string): drop or concat
        logits_temperature (float):
        num_stack (int): the number of frames to stack
        num_skip (int): the number of frames to skip
        splice (int): frames to splice. Default is 1 frame.
        input_channel (int): the number of channels of input features
        conv_channels (list):
        conv_kernel_sizes (list):
        conv_strides (list):
        poolings (list):
        activation (string): The activation function of CNN layers.
            Choose from relu or prelu or hard_tanh or maxout
        batch_norm (bool):
        label_smoothing_prob (float):
        weight_noise_std (float):
        encoder_residual (bool):
        encoder_dense_residual (bool):
    """

    def __init__(self,
                 input_size,
                 encoder_type,
                 encoder_bidirectional,
                 encoder_num_units,
                 encoder_num_proj,
                 encoder_num_layers,
                 fc_list,
                 dropout_input,
                 dropout_encoder,
                 num_classes,
                 parameter_init_distribution='uniform',
                 parameter_init=0.1,
                 recurrent_weight_orthogonal=False,
                 init_forget_gate_bias_with_one=True,
                 subsample_list=[],
                 subsample_type='drop',
                 logits_temperature=1,
                 num_stack=1,
                 num_skip=1,
                 splice=1,
                 input_channel=1,
                 conv_channels=[],
                 conv_kernel_sizes=[],
                 conv_strides=[],
                 poolings=[],
                 activation='relu',
                 batch_norm=False,
                 label_smoothing_prob=0,
                 weight_noise_std=0,
                 encoder_residual=False,
                 encoder_dense_residual=False):

        super(ModelBase, self).__init__()
        self.model_type = 'ctc'

        # Setting for the encoder
        self.input_size = input_size
        self.num_stack = num_stack
        self.num_skip = num_skip
        self.splice = splice
        self.encoder_type = encoder_type
        self.encoder_num_units = encoder_num_units
        if encoder_bidirectional:
            self.encoder_num_units *= 2
        self.fc_list = fc_list
        self.subsample_list = subsample_list

        # Setting for CTC
        self.num_classes = num_classes + 1  # Add the blank class
        self.logits_temperature = logits_temperature

        # Setting for regualarization
        self.weight_noise_injection = False
        self.weight_noise_std = float(weight_noise_std)
        self.ls_prob = label_smoothing_prob

        # Call the encoder function
        if encoder_type in ['lstm', 'gru', 'rnn']:
            self.encoder = load(encoder_type=encoder_type)(
                input_size=input_size,
                rnn_type=encoder_type,
                bidirectional=encoder_bidirectional,
                num_units=encoder_num_units,
                num_proj=encoder_num_proj,
                num_layers=encoder_num_layers,
                dropout_input=dropout_input,
                dropout_hidden=dropout_encoder,
                subsample_list=subsample_list,
                subsample_type=subsample_type,
                batch_first=True,
                merge_bidirectional=False,
                pack_sequence=True,
                num_stack=num_stack,
                splice=splice,
                input_channel=input_channel,
                conv_channels=conv_channels,
                conv_kernel_sizes=conv_kernel_sizes,
                conv_strides=conv_strides,
                poolings=poolings,
                activation=activation,
                batch_norm=batch_norm,
                residual=encoder_residual,
                dense_residual=encoder_dense_residual,
                nin=0)
        elif encoder_type == 'cnn':
            assert num_stack == 1 and splice == 1
            self.encoder = load(encoder_type='cnn')(
                input_size=input_size,
                input_channel=input_channel,
                conv_channels=conv_channels,
                conv_kernel_sizes=conv_kernel_sizes,
                conv_strides=conv_strides,
                poolings=poolings,
                dropout_input=dropout_input,
                dropout_hidden=dropout_encoder,
                activation=activation,
                batch_norm=batch_norm)
        else:
            raise NotImplementedError

        ##################################################
        # Fully-connected layers
        ##################################################
        if len(fc_list) > 0:
            for i in range(len(fc_list)):
                if i == 0:
                    if encoder_type == 'cnn':
                        bottle_input_size = self.encoder.output_size
                    else:
                        bottle_input_size = self.encoder_num_units

                    # if batch_norm:
                    #     setattr(self, 'bn_fc_0', nn.BatchNorm1d(
                    #         bottle_input_size))

                    setattr(self, 'fc_0', LinearND(
                        bottle_input_size, fc_list[i],
                        dropout=dropout_encoder))
                else:
                    # if batch_norm:
                    #     setattr(self, 'fc_bn_' + str(i),
                    #             nn.BatchNorm1d(fc_list[i - 1]))

                    setattr(self, 'fc_' + str(i), LinearND(
                        fc_list[i - 1], fc_list[i],
                        dropout=dropout_encoder))
            # TODO: remove a bias term in the case of batch normalization

            self.fc_out = LinearND(fc_list[-1], self.num_classes)
        else:
            self.fc_out = LinearND(self.encoder_num_units, self.num_classes)

        ##################################################
        # Initialize parameters
        ##################################################
        self.init_weights(parameter_init,
                          distribution=parameter_init_distribution,
                          ignore_keys=['bias'])

        # Initialize all biases with 0
        self.init_weights(0, distribution='constant', keys=['bias'])

        # Recurrent weights are orthogonalized
        if recurrent_weight_orthogonal and encoder_type != 'cnn':
            self.init_weights(parameter_init,
                              distribution='orthogonal',
                              keys=[encoder_type, 'weight'],
                              ignore_keys=['bias'])

        # Initialize bias in forget gate with 1
        if init_forget_gate_bias_with_one:
            self.init_forget_gate_bias_with_one()

        # Set CTC decoders
        self._decode_greedy_np = GreedyDecoder(blank_index=0)
        self._decode_beam_np = BeamSearchDecoder(blank_index=0)
        # NOTE: index 0 is reserved for the blank class in warpctc_pytorch
        # TODO: set space index

    def forward(self, xs, ys, is_eval=False):
        """Forward computation.
        Args:
            xs (list): A list of length `[B]`, which contains arrays of size `[T, input_size]`
            ys (list): A list of length `[B]`, which contains arrays of size `[L]`
            is_eval (bool): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
        Returns:
            loss (torch.autograd.Variable(float)): A tensor of size `[1]`
        """
        if is_eval:
            self.eval()
        else:
            self.train()

            # Gaussian noise injection
            if self.weight_noise_injection:
                self.inject_weight_noise(mean=0, std=self.weight_noise_std)

        # Sort by lenghts in the descending order
        if is_eval and self.encoder_type != 'cnn':
            perm_idx = sorted(list(range(0, len(xs), 1)),
                              key=lambda i: xs[i].shape[0], reverse=True)
            xs = [xs[i] for i in perm_idx]
            ys = [ys[i] for i in perm_idx]
            # NOTE: must be descending order for pack_padded_sequence
            # NOTE: assumed that xs is already sorted in the training stage

        # Frame stacking
        if self.num_stack > 1:
            xs = [stack_frame(x, self.num_stack, self.num_skip)
                  for x in xs]

        # Splicing
        if self.splice > 1:
            xs = [do_splice(x, self.splice, self.num_stack) for x in xs]

        # Wrap by Variable
        xs = [np2var(x, self.device_id).float() for x in xs]
        x_lens = [len(x) for x in xs]

        # Encode acoustic features
        logits, x_lens = self._encode(xs, x_lens)

        # Output smoothing
        if self.logits_temperature != 1:
            logits /= self.logits_temperature

        # Wrap by Variable
        ys = [np2var(np.fromiter(y, dtype=np.int64), self.device_id).long()
              for y in ys]
        _x_lens = np2var(np.fromiter(x_lens, dtype=np.int32), -1).int()
        y_lens = np2var(np.fromiter([y.size(0)
                                     for y in ys], dtype=np.int32), -1).int()
        # NOTE: do not copy to GPUs

        # Concatenate all elements in ys for warpctc_pytorch
        ys = torch.cat(ys, dim=0).cpu().int() + 1
        # NOTE: index 0 is reserved for blank in warpctc_pytorch

        # Compute CTC loss
        loss = my_warpctc(logits.transpose(0, 1),  # time-major
                          ys, _x_lens, y_lens,
                          size_average=False) / len(xs)

        # loss = warpctc(logits.transpose(0, 1),  # time-major
        #                ys, _x_lens, y_lens) / len(xs)

        if self.device_id >= 0:
            loss = loss.cuda(self.device_id)

        # Label smoothing (with uniform distribution)
        if self.ls_prob > 0:
            loss_ls = cross_entropy_label_smoothing(
                logits,
                y_lens=x_lens,  # NOTE: CTC is frame-synchronous
                label_smoothing_prob=self.ls_prob,
                distribution='uniform',
                size_average=False) / len(xs)
            loss = loss * (1 - self.ls_prob) + loss_ls

        return loss

    def _encode(self, xs, x_lens, is_multi_task=False):
        """Encode acoustic features.
        Args:
            xs (list): A list of length `[B]`, which contains Variables of size `[T, input_size]`
            x_lens (list): A list of length `[B]`
            is_multi_task (bool):
        Returns:
            xs (torch.autograd.Variable, float): A tensor of size
                `[B, T, encoder_num_units]`
            x_lens (list): A tensor of size `[B]`
            OPTION:
                xs_sub (torch.autograd.Variable, float): A tensor of size
                    `[B, T, encoder_num_units]`
                x_lens_sub (list): A tensor of size `[B]`
        """
        # Convert list to Variables
        xs = pad_list(xs)

        if is_multi_task:
            if self.encoder_type == 'cnn':
                xs, x_lens = self.encoder(xs, x_lens)
                xs_sub = xs.clone()
                x_lens_sub = copy.deepcopy(x_lens)
            else:
                xs, x_lens, xs_sub, x_lens_sub = self.encoder(
                    xs, x_lens, volatile=not self.training)
        else:
            if self.encoder_type == 'cnn':
                xs, x_lens = self.encoder(xs, x_lens)
            else:
                xs, x_lens = self.encoder(
                    xs, x_lens, volatile=not self.training)

        # Path through fully-connected layers
        if len(self.fc_list) > 0:
            for i in range(len(self.fc_list)):
                # if self.batch_norm:
                #     xs = getattr(self, 'bn_fc_' + str(i))(xs)
                xs = getattr(self, 'fc_' + str(i))(xs)
        logits = self.fc_out(xs)

        if is_multi_task:
            # Path through fully-connected layers
            for i in range(len(self.fc_list_sub)):
                # if self.batch_norm:
                #     xs_sub = getattr(self, 'bn_fc_sub_' + str(i))(xs_sub)
                xs_sub = getattr(self, 'fc_sub_' + str(i))(xs_sub)
            logits_sub = self.fc_out_sub(xs_sub)

            return logits, x_lens, logits_sub, x_lens_sub
        else:
            return logits, x_lens

    def decode(self, xs, beam_width, max_decode_len=None,
               min_decode_len=0, length_penalty=0, coverage_penalty=0, task_index=0):
        """CTC decoding.
        Args:
            xs (list): A list of length `[B]`, which contains arrays of size `[T, input_size]`
            beam_width (int): the size of beam
            max_decode_len: not used
            min_decode_len: not used
            length_penalty: not used
            coverage_penalty: not used
            task_index (bool): the index of a task
        Returns:
            best_hyps (list): A list of length `[B]`, which contains arrays of size `[L]`
            None: this corresponds to aw in attention-based models
            perm_idx (list): A list of length `[B]`
        """
        self.eval()

        # Sort by lenghts in the descending order
        if self.encoder_type != 'cnn':
            perm_idx = sorted(list(range(0, len(xs), 1)),
                              key=lambda i: xs[i].shape[0], reverse=True)
            xs = [xs[i] for i in perm_idx]
            # NOTE: must be descending order for pack_padded_sequence
        else:
            perm_idx = list(range(0, len(xs), 1))

        # Frame stacking
        if self.num_stack > 1:
            xs = [stack_frame(x, self.num_stack, self.num_skip)
                  for x in xs]

        # Splicing
        if self.splice > 1:
            xs = [do_splice(x, self.splice, self.num_stack) for x in xs]

        # Wrap by Variable
        xs = [np2var(x, self.device_id, volatile=True).float() for x in xs]
        x_lens = [len(x) for x in xs]

        # Encode acoustic features
        if hasattr(self, 'main_loss_weight'):
            if task_index == 0:
                logits, x_lens, _, _ = self._encode(
                    xs, x_lens, is_multi_task=True)
            elif task_index == 1:
                _, _, logits, x_lens = self._encode(
                    xs, x_lens, is_multi_task=True)
            else:
                raise NotImplementedError
        else:
            logits, x_lens = self._encode(xs, x_lens)

        if beam_width == 1:
            best_hyps = self._decode_greedy_np(var2np(logits), x_lens)
        else:
            best_hyps = self._decode_beam_np(
                var2np(F.log_softmax(logits, dim=-1)),
                x_lens, beam_width=beam_width)

        # NOTE: index 0 is reserved for the blank class in warpctc_pytorch
        best_hyps -= 1

        return best_hyps, None, perm_idx
        # NOTE: None corresponds to aw in attention-based models

    def posteriors(self, xs, temperature=1, blank_scale=None, task_idx=0):
        """Returns CTC posteriors (after the softmax layer).
        Args:
            xs (list): A list of length `[B]`, which contains arrays of size `[T, input_size]`
            temperature (float): the temperature parameter for the
                softmax layer in the inference stage
            blank_scale (float):
            task_idx (int): the index ofta task
        Returns:
            probs (np.ndarray): A tensor of size `[B, T, num_classes]`
            x_lens (list): A list of length `[B]`
            perm_idx (list): A list of length `[B]`
        """
        self.eval()

        # Sort by lenghts in the descending order
        if self.encoder_type != 'cnn':
            perm_idx = sorted(list(range(0, len(xs), 1)),
                              key=lambda i: xs[i].shape[0], reverse=True)
            xs = [xs[i] for i in perm_idx]
            # NOTE: must be descending order for pack_padded_sequence
        else:
            perm_idx = list(range(0, len(xs), 1))

        # Frame stacking
        if self.num_stack > 1:
            xs = [stack_frame(x, self.num_stack, self.num_skip)
                  for x in xs]

        # Splicing
        if self.splice > 1:
            xs = [do_splice(x, self.splice, self.num_stack) for x in xs]

        # Wrap by Variable
        xs = [np2var(x, self.device_id, volatile=True).float() for x in xs]
        x_lens = [len(x) for x in xs]

        # Encode acoustic features
        if hasattr(self, 'main_loss_weight'):
            if task_idx == 0:
                logits, x_lens, _, _ = self._encode(
                    xs, x_lens, is_multi_task=True)
            elif task_idx == 1:
                _, _, logits, x_lens = self._encode(
                    xs, x_lens, is_multi_task=True)
            else:
                raise NotImplementedError
        else:
            logits, x_lens = self._encode(xs, x_lens)

        probs = F.softmax(logits / temperature, dim=-1)

        # Divide by blank prior
        if blank_scale is not None:
            raise NotImplementedError

        return var2np(probs), x_lens, perm_idx

    def decode_from_probs(self, probs, x_lens, beam_width=1,
                          max_decode_len=None):
        """
        Args:
            probs (np.ndarray):
            x_lens (np.ndarray):
            beam_width (int):
            max_decode_len (int):
        Returns:
            best_hyps (np.ndarray):
        """
        # TODO: Subsampling

        # Convert to log-scale
        log_probs = np.log(probs + 1e-10)

        if beam_width == 1:
            best_hyps = self._decode_greedy_np(log_probs, x_lens)
        else:
            best_hyps = self._decode_beam_np(
                log_probs, x_lens, beam_width=beam_width)

        # NOTE: index 0 is reserved for the blank class in warpctc_pytorch
        best_hyps -= 1

        return best_hyps
