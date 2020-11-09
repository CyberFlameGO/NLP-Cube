#
# Author: Tiberiu Boros
#
# Copyright (c) 2020 Adobe Systems Incorporated. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import torch
import sys

sys.path.append('')
import torch.nn as nn
from cube2.io_utils.encodings import Encodings
from cube2.io_utils.config import MLMConfig
import numpy as np
import optparse
import random
import json
from tqdm import tqdm
from cube2.networks.modules import ConvNorm


class Dataset:
    def __init__(self):
        self.sequences = []
        self.lang2cluster = {}
        self.equivalents = {}

    def load_dataset(self, filename, lang_id):
        lines = open(filename).readlines()
        for line in tqdm(lines, desc='\t{0}'.format(filename), ncols=100):
            parts = line.strip().split(' ')
            self.sequences.append([parts, lang_id])

    def load_dict(self, src, dst, file):
        f = open(file)
        for line in f.readlines():
            line = line.strip()
            if not line.startswith("#"):
                parts = line.split('\t')
                if len(parts) < 2:
                    print(line)
                    continue
                src_words = parts[0].split(';')
                dst_words = parts[1].split(';')
                for w_src in src_words:
                    w_src = w_src.strip()
                    if ' ' not in w_src:
                        if (w_src, src) not in self.equivalents:
                            self.equivalents[(w_src, src)] = []
                        lst = self.equivalents[(w_src, src)]
                        for w_dst in dst_words:
                            w_dst = w_dst.strip()
                            if ' ' not in w_dst:
                                lst.append((w_dst, dst))
                for w_src in dst_words:
                    w_src = w_src.strip()
                    if ' ' not in w_src:
                        if (w_src, dst) not in self.equivalents:
                            self.equivalents[(w_src, dst)] = []
                        lst = self.equivalents[(w_src, dst)]
                        for w_dst in src_words:
                            w_dst = w_dst.strip()
                            if ' ' not in w_dst:
                                lst.append((w_dst, src))

        f.close()


class Encodings:
    def __init__(self, filename=None):
        # self._token2int = {}
        self._char2int = {}
        self._num_langs = 0
        if filename is not None:
            self.load(filename)

    def save(self, filename):
        json_obj = {'num_langs': self._num_langs,
                    'char2int': self._char2int}
        json.dump(json_obj, open(filename, 'w'))

    def load(self, filename):
        json_obj = json.load(open(filename))
        # self._token2int = json_obj['token2int']
        self._char2int = json_obj['char2int']
        self._num_langs = json_obj['num_langs']

    def compute_encodings(self, dataset: Dataset, w_cutoff=7, ch_cutoff=7):
        char2count = {}
        for example in dataset.sequences:
            seq = example[0]
            lang_id = example[1]
            if lang_id + 1 > self._num_langs:
                self._num_langs = lang_id + 1
            for token in seq:
                for char in token:
                    ch = char.lower()
                    if ch not in char2count:
                        char2count[ch] = 1
                    else:
                        char2count[ch] += 1

        self._char2int = {'<PAD>': 0, '<UNK>': 1, 'START': 2, 'STOP': 3}
        for char in char2count:
            if char2count[char] >= ch_cutoff:
                self._char2int[char] = len(self._char2int)

    def __str__(self):
        result = "\t::Holistic chars: {0}\n\t::Languages: {1}".format(
            len(self._char2int), self._num_langs)
        return result


class WordGram(nn.Module):
    def __init__(self, encodings: Encodings):
        super(WordGram, self).__init__()
        NUM_FILTERS = 512
        self._num_filters = NUM_FILTERS
        self._encodings = encodings
        self._lang_emb = nn.Embedding(encodings._num_langs, 32)
        self._tok_emb = nn.Embedding(len(encodings._char2int), 256)
        self._case_emb = nn.Embedding(4, 32)
        convolutions_char = []
        cs_inp = 256 + 32 + 32
        for _ in range(3):
            conv_layer = nn.Sequential(
                ConvNorm(cs_inp,
                         NUM_FILTERS,
                         kernel_size=5, stride=1,
                         padding=2,
                         dilation=1, w_init_gain='tanh'),
                nn.BatchNorm1d(NUM_FILTERS))
            convolutions_char.append(conv_layer)
            cs_inp = NUM_FILTERS // 2
        self._convolutions_char = nn.ModuleList(convolutions_char)

    def forward(self, words, langs):
        x_char, x_case, x_lang = self._make_data(words, langs)

        x_char = self._tok_emb(x_char)
        x_case = self._case_emb(x_case)
        x_lang = self._lang_emb(x_lang)

        x = torch.cat([x_char, x_lang.unsqueeze(1).repeat(1, x_case.shape[1], 1), x_case], dim=-1)
        x = x.permute(0, 2, 1)
        cnt = 0
        half = self._num_filters // 2
        for conv in self._convolutions_char:
            drop = self.training
            if cnt >= len(self._convolutions_char):
                drop = False
            conv_out = conv(x)
            tmp = torch.tanh(conv_out[:, :half, :]) * torch.sigmoid((conv_out[:, half:, :]))
            x = torch.dropout(tmp, 0.1, drop)
        x = x.permute(0, 2, 1)
        # scaled tanh output - avoids -inf/nan loss
        return 5.0 * torch.tanh(torch.sum(x, dim=1))

    def _make_data(self, words, langs):
        x_char = np.zeros((len(words), max([len(w) for w in words]) + 2))
        x_case = np.zeros((x_char.shape[0], x_char.shape[1]))

        for ii in range(x_char.shape[0]):
            x_char[ii, 0] = 2
            x_case[ii, 0] = 1
            x_char[ii, len(words[ii]) + 1] = 3
            x_case[ii, len(words[ii]) + 1] = 1
            for jj in range(x_char.shape[1]):
                if jj < len(words[ii]):
                    ch = words[ii][jj].lower()
                    if ch in self._encodings._char2int:
                        x_char[ii, jj + 1] = self._encodings._char2int[ch]
                    else:
                        x_char[ii, jj + 1] = 1  # UNK
                    if ch.lower() == ch.upper():
                        x_case[ii, jj + 1] = 1
                    elif ch.lower() != ch:
                        x_case[ii, jj + 1] = 2
                    else:
                        x_case[ii, jj + 1] = 3

        x_char = np.copy(np.flip(x_char, axis=1))
        x_case = np.copy(np.flip(x_case, axis=1))

        x_langs = np.array(langs)

        return torch.tensor(x_char, device=self._get_device(), dtype=torch.long), \
               torch.tensor(x_case, device=self._get_device(), dtype=torch.long), \
               torch.tensor(x_langs, device=self._get_device(), dtype=torch.long)

    def _get_device(self):
        if self._lang_emb.weight.device.type == 'cpu':
            return 'cpu'
        return '{0}:{1}'.format(self._lang_emb.weight.device.type, str(self._lang_emb.weight.device.index))

    def save(self, path):
        torch.save(self.state_dict(), path)

    def load(self, path):
        self.load_state_dict(torch.load(path, map_location='cpu'))


class SkipgramDataset:
    def __init__(self, dataset: Dataset, encodings: Encodings, win_size=2, w_cutoff=7):
        from cube2.networks.alg import FlatMap
        self.encodings = encodings
        self._word_id = -1
        self._word_list = []
        self._word2pos = []
        self._word2int = {}
        self._lang2widx = {}
        self._equivalents = dataset.equivalents
        sys.stdout.write("\t::Computing stats\n")
        sys.stdout.flush()
        w_list = {}
        for ii in range(len(dataset.sequences)):
            seq = dataset.sequences[ii][0]
            lang_id = dataset.sequences[ii][1]
            for word in seq:
                if (word, lang_id) not in w_list:
                    w_list[(word, lang_id)] = 1
                else:
                    w_list[(word, lang_id)] += 1

        targets = []
        for (w, l) in w_list:
            if w_list[(w, l)] >= w_cutoff:
                self._word_list.append([w, l])
                self._word2int[(w, l)] = len(self._word2int)
                self._word2pos.append(FlatMap())
                targets.append([])

        for ii in tqdm(range(len(dataset.sequences)), desc="\t\t", ncols=80):
            seq = dataset.sequences[ii][0]
            lang = dataset.sequences[ii][1]
            for ii in range(len(seq)):
                word = seq[ii]

                if (word, lang) in self._word2int:
                    w_index1 = self._word2int[(word, lang)]
                    for jj in range(max(0, ii - win_size), min(len(seq) - 1, ii + win_size + 2)):
                        if ii != jj:
                            ww = seq[jj]
                            if (ww, lang) in self._word2int:
                                w_index2 = self._word2int[(ww, lang)]
                                source = w_index1
                                dest = w_index2
                                # if w_index2 < w_index1:
                                #     source = w_index2
                                #     dest = w_index1
                                targets[source].append(dest)

        # convert to probs
        sys.stdout.write("\t::Converting to probs\n")
        sys.stdout.flush()
        self._train_idx = []
        for w_index in tqdm(range(len(targets)), desc="\t\t", ncols=80):
            tgt_list = list(sorted(targets[w_index]))
            occ = [1]
            for ii in range(1, len(tgt_list)):
                if tgt_list[ii] == tgt_list[ii - 1]:
                    occ.append(occ[-1] + 1)
                else:
                    occ.append(1)

            new_targets = []
            new_occ = []
            total = 0
            for ii in range(len(tgt_list)):
                if ii == len(tgt_list) - 1 or occ[ii] >= occ[ii + 1]:
                    new_targets.append(tgt_list[ii])
                    new_occ.append(occ[ii])
                    total += new_occ[-1]
            for ii in range(len(new_occ)):
                new_occ[ii] = new_occ[ii] / total

            del tgt_list
            del occ
            last_prob = 0
            for ii in range(len(new_occ)):
                # if new_occ[ii] > 0.01:
                fm = self._word2pos[w_index]
                fm._keys.append(new_targets[ii])
                fm._objects.append(new_occ[ii] + last_prob)
                last_prob += new_occ[ii]
            if len(new_occ) > 0:
                self._train_idx.append(w_index)
            del new_targets
            del new_occ

            targets[w_index] = []

        del targets
        sys.stdout.write("\t::Computing lang lookups\n")
        sys.stdout.flush()

        for w_index in range(len(self._word2pos)):
            lang = self._word_list[w_index][1]
            if lang not in self._lang2widx:
                self._lang2widx[lang] = []
            self._lang2widx[lang].append(w_index)

    def get_count(self):
        return len(self._train_idx)

    def shuffle(self):
        random.shuffle(self._train_idx)

    def reset(self):
        self._word_id = -1

    def _pos_sample_n(self, probs, n_samples):

        def _get_item(keys, probs, prob):
            start = 0
            end = len(keys) - 1
            while start < end:
                mid = (start + end) // 2
                max_prob = probs[mid]
                min_prob = 0
                if mid > 0:
                    min_prob = probs[mid - 1]
                if min_prob <= prob and max_prob >= prob:
                    return keys[mid]

                if max_prob <= prob:
                    start = mid + 1
                if min_prob >= prob:
                    end = mid - 1
            return keys[0]

        samp = []
        while (len(samp) < n_samples):
            rnd = random.random()
            samp.append(_get_item(probs._keys, probs._objects, rnd))
        return samp

    def _neg_sample_n(self, probs, lang_word_idx, n_samples):
        samp = []
        n = len(lang_word_idx) - 1
        while len(samp) < n_samples:
            w_idx = lang_word_idx[random.randint(0, n)]
            if w_idx not in probs:
                samp.append(w_idx)
        return samp

    def get_next_batch(self, batch_size=128):
        x = []
        y_pos = []
        y_pos_t = []
        y_neg = []
        l = []

        while len(x) < batch_size and self._word_id < len(self._train_idx) - 1:
            self._word_id += 1
            w_idx = self._train_idx[self._word_id]
            word = self._word_list[w_idx][0]
            lang_id = self._word_list[w_idx][1]
            x.append(word)
            l.append(lang_id)
            y_t = self._pos_sample_n(self._word2pos[w_idx], 4)
            y_pos.append([self._word_list[ii][0] for ii in y_t])
            if (word, lang_id) in self._equivalents:
                y_pos_t.append(self._equivalents[(word, lang_id)])
            else:
                y_pos_t.append([])

            # negative examples
            y_t2 = self._neg_sample_n(self._word2pos[w_idx], self._lang2widx[lang_id], 4)
            y_neg.append([self._word_list[ii][0] for ii in y_t2])
        return x, y_pos, y_pos_t, y_neg, l


def log1pexp(x):
    return torch.where(x < 50, torch.log1p(torch.exp(x)), x)


def _eval(model, sdev, criterion, batch_size):
    total_loss = 0
    model.eval()
    num_batches = sdev.get_count() // params.batch_size
    if sdev.get_count() % params.batch_size != 0:
        num_batches += 1
    epoch_loss = 0
    import tqdm
    model.train()
    pgb = tqdm.tqdm(range(num_batches), desc='\tevaluating', ncols=160)
    for batch_idx in pgb:
        x, y_pos, y_pos_t, y_neg, l = sdev.get_next_batch(batch_size=batch_size)
        x2pos = {}
        x2neg = {}
        words = []
        langs = []
        w_index = 0
        cnt = 0
        for word in x:
            words.append(word)
            langs.append(l[cnt])
            x2pos[w_index] = []
            x2neg[w_index] = []
            for ww in y_pos[cnt]:
                x2pos[w_index].append(len(words))
                words.append(ww)
                langs.append(l[cnt])
            for ww in y_neg[cnt]:
                x2neg[w_index].append(len(words))
                words.append(ww)
                langs.append(l[cnt])
            for (ww, l_id) in y_pos_t[cnt]:
                x2pos[w_index].append(len(words))
                words.append(ww)
                langs.append(l_id)

            w_index = len(words)
            cnt += 1

        repr = model(words, langs)

        x_list = []
        pos_list = []
        for w_index in x2pos:
            index1 = w_index
            for index2 in x2pos[w_index]:
                x_list.append(repr[index1].unsqueeze(0))
                pos_list.append(repr[index2].unsqueeze(0))
        x_list = torch.cat(x_list, dim=0)
        pos_list = torch.cat(pos_list, dim=0)
        tmp = x_list * pos_list
        tmp = torch.mean(tmp, dim=1)
        tmp = log1pexp(-tmp)  # torch.log(1 + torch.exp(-tmp))
        loss_pos = tmp.mean()

        x_list = []
        neg_list = []
        for w_index in x2pos:
            index1 = w_index
            for index2 in x2neg[w_index]:
                x_list.append(repr[index1].unsqueeze(0))
                neg_list.append(repr[index2].unsqueeze(0))
        x_list = torch.cat(x_list, dim=0)
        neg_list = torch.cat(neg_list, dim=0)
        tmp = x_list * neg_list
        tmp = torch.mean(tmp, dim=1)
        tmp = log1pexp(tmp)  # torch.log(1 + torch.exp(tmp))
        loss_neg = tmp.mean()

        loss = loss_pos + loss_neg
        total_loss += loss.item()

    return total_loss / num_batches


def do_train(params):
    training_conf = json.load(open(params.train_file))
    ds_list = training_conf['langs']
    train_list = []
    dev_list = []
    lang2id = {}
    for ii in range(len(ds_list)):
        train_list.append(ds_list[ii][1])
        dev_list.append(ds_list[ii][2])
        lang2id[ds_list[ii][0]] = len(lang2id)

    trainset = Dataset()
    devset = Dataset()
    sys.stdout.write('STEP 1: Loading data\n')
    for ii, train, dev in zip(range(len(train_list)), train_list, dev_list):
        sys.stdout.write('\tLoading language {0}\n'.format(ii))
        trainset.load_dataset(train_list[ii], ii)
        devset.load_dataset(dev_list[ii], ii)

    dicts = training_conf['dicts']
    for dict in dicts:
        l_src = lang2id[dict[0]]
        l_dst = lang2id[dict[1]]
        file = dict[2]
        trainset.load_dict(l_src, l_dst, file)

    sys.stdout.write('STEP 2: Computing encodings\n')
    encodings = Encodings()
    encodings.compute_encodings(trainset)
    print(encodings)
    encodings.save('{0}.encodings'.format(params.store))
    sys.stdout.write('STEP 3: Building training and test data\n')
    strain = SkipgramDataset(trainset, encodings, win_size=5)
    sdev = SkipgramDataset(devset, encodings, win_size=5, w_cutoff=2)
    del trainset
    del devset

    model = WordGram(encodings)
    if params.device != 'cpu':
        model.to(params.device)

    import torch.optim as optim
    import torch.nn as nn
    optimizer = optim.Adam(model.parameters(), lr=1e-4)  # , betas=(0.9, 0.9))
    criterion = nn.CrossEntropyLoss()

    patience_left = params.patience
    epoch = 1
    sys.stdout.write('STEP 3: Starting training process\n')
    best_nll = 9999
    # nll = _eval(model, sdev, criterion, params.batch_size)
    while patience_left > 0:
        patience_left -= 1
        sys.stdout.write('\n\nStarting epoch ' + str(epoch) + '\n')
        sys.stdout.flush()
        num_batches = strain.get_count() // params.batch_size
        if strain.get_count() % params.batch_size != 0:
            num_batches += 1
        epoch_loss = 0
        import tqdm
        model.train()
        strain.shuffle()
        pgb = tqdm.tqdm(range(num_batches), desc='\tloss=NaN h=N/A w=N/A', ncols=160)
        for batch_idx in pgb:
            x, y_pos, y_pos_t, y_neg, l = strain.get_next_batch(batch_size=params.batch_size)
            x2pos = {}
            x2neg = {}
            words = []
            langs = []
            w_index = 0
            cnt = 0
            for word in x:
                words.append(word)
                langs.append(l[cnt])
                x2pos[w_index] = []
                x2neg[w_index] = []
                for ww in y_pos[cnt]:
                    x2pos[w_index].append(len(words))
                    words.append(ww)
                    langs.append(l[cnt])
                for ww in y_neg[cnt]:
                    x2neg[w_index].append(len(words))
                    words.append(ww)
                    langs.append(l[cnt])

                for (ww, l_id) in y_pos_t[cnt]:
                    x2pos[w_index].append(len(words))
                    words.append(ww)
                    langs.append(l_id)

                w_index = len(words)
                cnt += 1

            repr = model(words, langs)

            x_list = []
            pos_list = []
            for w_index in x2pos:
                index1 = w_index
                for index2 in x2pos[w_index]:
                    x_list.append(repr[index1].unsqueeze(0))
                    pos_list.append(repr[index2].unsqueeze(0))
            x_list = torch.cat(x_list, dim=0)
            pos_list = torch.cat(pos_list, dim=0)
            tmp = x_list * pos_list
            tmp = torch.mean(tmp, dim=1)
            tmp = log1pexp(-tmp)  # torch.log(1 + torch.exp(-tmp))
            loss_pos = tmp.mean()

            x_list = []
            neg_list = []
            for w_index in x2pos:
                index1 = w_index
                for index2 in x2neg[w_index]:
                    x_list.append(repr[index1].unsqueeze(0))
                    neg_list.append(repr[index2].unsqueeze(0))
            x_list = torch.cat(x_list, dim=0)
            neg_list = torch.cat(neg_list, dim=0)
            tmp = x_list * neg_list
            tmp = torch.mean(tmp, dim=1)
            tmp = log1pexp(tmp)  # torch.log(1 + torch.exp(tmp))
            loss_neg = tmp.mean()

            loss = loss_pos + loss_neg
            optimizer.zero_grad()
            loss.backward()
            epoch_loss += loss.item()
            optimizer.step()
            pgb.set_description(
                '\tloss={0:.4f} p={1:.4f} n={2:.4f}'.format(loss.item(), loss_pos.item(), loss_neg.item()))

            if (batch_idx + 1) % 10000 == 0:
                sys.stdout.write('\n')
                sdev.reset()
                nll = _eval(model, sdev, criterion, params.batch_size)

                sys.stdout.write('\tStoring {0}-skip.last\n'.format(params.store))
                sys.stdout.flush()
                fn = '{0}-skip.last'.format(params.store)
                model.save(fn)
                sys.stderr.flush()
                if best_nll > nll:
                    best_nll = nll
                    sys.stdout.write('\tStoring {0}-skip.bestNLL\n'.format(params.store))
                    sys.stdout.flush()
                    fn = '{0}-skip.bestNLL'.format(params.store)
                    model.save(fn)
                    patience_left = params.patience

                sys.stdout.write(
                    "\tValidation NLL={0:.4f}\n".format(nll))
                sys.stdout.flush()

        strain.reset()
        sdev.reset()
        nll = _eval(model, sdev, criterion, params.batch_size)

        sys.stdout.write('\tStoring {0}-skip.last\n'.format(params.store))
        sys.stdout.flush()
        fn = '{0}-skip.last'.format(params.store)
        model.save(fn)
        sys.stderr.flush()
        if best_nll > nll:
            best_nll = nll
            sys.stdout.write('\tStoring {0}-skip.bestNLL\n'.format(params.store))
            sys.stdout.flush()
            fn = '{0}-skip.bestNLL'.format(params.store)
            model.save(fn)
            patience_left = params.patience

        sys.stdout.write("\tAVG Epoch loss = {0:.6f}\n".format(epoch_loss / num_batches))
        sys.stdout.flush()
        sys.stdout.write(
            "\tValidation NLL={0:.4f}\n".format(nll))
        sys.stdout.flush()
        epoch += 1


if __name__ == '__main__':
    parser = optparse.OptionParser()
    parser.add_option('--train', action='store', dest='train_file',
                      help='Start building a tagger model')
    parser.add_option('--config', action='store', dest='config_file',
                      help='Use this configuration file for tagger')
    parser.add_option('--patience', action='store', type='int', default=20, dest='patience',
                      help='Number of epochs before early stopping (default=20)')
    parser.add_option('--store', action='store', dest='store', help='Output base', default='mlm')
    parser.add_option('--batch-size', action='store', type='int', default=32, dest='batch_size',
                      help='Number of epochs before early stopping (default=32)')
    parser.add_option('--device', action='store', dest='device', default='cpu',
                      help='What device to use for models: cpu, cuda:0, cuda:1 ...')

    (params, _) = parser.parse_args(sys.argv)

    if params.train_file:
        do_train(params)
