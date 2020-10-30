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

    def load_dataset(self, filename, lang_id):
        lines = open(filename).readlines()
        for line in tqdm(lines, desc='\t{0}'.format(filename), ncols=100):
            parts = line.strip().split(' ')
            self.sequences.append([parts, lang_id])

    def load_clusters(self, filename, lang_id):
        lines = open(filename).readlines()
        self.lang2cluster[lang_id] = {}
        for ii in tqdm(range(len(lines) // 4), desc='\t{0}'.format(filename), ncols=100):
            self.lang2cluster[lang_id][ii] = lines[ii * 4 + 1].split(' ')


class Encodings:
    def __init__(self, filename=None):
        self._token2int = {}
        self._char2int = {}
        self._num_langs = 0
        self._word2target = {}
        self._max_clusters = 0
        self._max_words_in_clusters = 0
        if filename is not None:
            self.load(filename)

    def save(self, filename):
        json_obj = {'num_langs': self._num_langs, 'max_clusters': self._max_clusters,
                    'max_words_in_clusters': self._max_words_in_clusters, 'token2int': self._token2int,
                    'char2int': self._char2int, '_word2target': self._word2target}
        json.dump(json_obj, open(filename, 'w'))

    def load(self, filename):
        json_obj = json.load(open(filename))
        self._token2int = json_obj['token2int']
        self._char2int = json_obj['char2int']
        self._num_langs = json_obj['num_langs']
        self._word2target = json_obj['_word2target']
        self._max_clusters = json_obj['max_clusters']
        self._max_words_in_clusters = json_obj['max_words_in_clusters']

    def compute_encodings(self, dataset: Dataset, w_cutoff=7, ch_cutoff=7):
        char2count = {}
        token2count = {}
        for example in dataset.sequences:
            seq = example[0]
            lang_id = example[1]
            if lang_id + 1 > self._num_langs:
                self._num_langs = lang_id + 1
            for token in seq:
                if token not in token2count:
                    tk = token.lower()
                    token2count[tk] = 1
                else:
                    token2count[tk] += 1
            for char in token:
                ch = char.lower()
                if ch not in char2count:
                    char2count[ch] = 1
                else:
                    char2count[ch] += 1

        self._char2int = {'<PAD>': 0, '<UNK>': 1}
        self._token2int = {'<PAD>': 0, '<UNK>': 1}
        for char in char2count:
            if char2count[char] >= ch_cutoff:
                self._char2int[char] = len(self._char2int)
        for token in token2count:
            if token2count[token] > w_cutoff:
                self._token2int[token] = len(self._token2int)
        self._word2target = {}
        for lang_id in dataset.lang2cluster:
            clusters = dataset.lang2cluster[lang_id]
            self._word2target[lang_id] = {}
            if len(clusters) > self._max_clusters:
                self._max_clusters = len(clusters)
            for cid in clusters:
                cluster = clusters[cid]
                if len(cluster) > self._max_words_in_clusters:
                    self._max_words_in_clusters = len(cluster)
                cnt = 0
                for word in cluster:
                    self._word2target[lang_id][word] = [cid, cnt]
                    cnt += 1

    def __str__(self):
        w2t_count = 0
        for lang_id in self._word2target:
            w2t_count += len(self._word2target[lang_id])
        result = "\t::Holistic tokens: {0}\n\t::Holistic chars: {1}\n\t::Max clusters: {2}\n\t::Max words in clusters: {3}\n\t::Languages: {4}\n\t::Known word targets: {5}".format(
            len(self._token2int), len(self._char2int), self._max_clusters, self._max_words_in_clusters, self._num_langs,
            w2t_count)
        return result


class WordGram(nn.Module):
    def __init__(self, encodings: Encodings):
        super(WordGram, self).__init__()
        NUM_FILTERS = 256
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
            cs_inp = NUM_FILTERS
        self._convolutions_char = nn.ModuleList(convolutions_char)

    def forward(self, words, langs):
        x_char, x_case, x_lang = self._make_data(words, langs)

        x_char = self._tok_emb(x_char)
        x_case = self._case_emb(x_case)
        x_lang = self._lang_emb(x_lang)

        x = torch.cat([x_char, x_lang.unsqueeze(1).repeat(1, x_case.shape[1], 1), x_case], dim=-1)
        x = x.permute(0, 2, 1)
        for conv in self._convolutions_char:
            x = torch.dropout(torch.tanh(conv(x)), 0.5, False)
        x = x.permute(0, 2, 1)

        return torch.sum(x, dim=1)

    def _make_data(self, words, langs):
        x_char = np.zeros((len(words), max([len(w) for w in words])))
        x_case = np.zeros((x_char.shape[0], x_char.shape[1]))

        for ii in range(x_char.shape[0]):
            for jj in range(x_char.shape[1]):
                if jj < len(words[ii]):
                    ch = words[ii][jj].lower()
                    if ch in self._encodings._char2int:
                        x_char[ii, jj] = self._encodings._char2int[ch]
                    else:
                        x_char[ii, jj] = 1  # UNK
                    if ch.lower() == ch.upper():
                        x_case[ii, jj] = 1
                    elif ch.lower() != ch:
                        x_case[ii, jj] = 2
                    else:
                        x_case[ii, jj] = 3

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
    def __init__(self, dataset: Dataset, encodings: Encodings):
        self.dataset = dataset
        self.encodings = encodings
        self._seq_id = 0
        self._word_id = -1
        self._total_examples = 0
        self._word_list = []
        w_list = {}
        for ii in range(len(dataset.sequences)):
            seq = dataset.sequences[ii][0]
            self._total_examples += len(seq)
            for word in seq:
                if word not in w_list:
                    w_list[word] = 1
                else:
                    w_list[word] += 1
        for w in w_list:
            if w_list[w] >= 7:
                self._word_list.append(w)

    def get_count(self):
        return self._total_examples

    def shuffle(self):
        random.shuffle(self.dataset.sequences)

    def reset(self):
        self._seq_id = 0
        self._word_id = -1

    def get_next_batch(self, batch_size=128):
        x = []
        y_pos = []
        y_neg = []
        l = []

        while len(x) < batch_size:
            self._word_id += 1
            if self._seq_id >= len(self.dataset.sequences):
                break
            if self._word_id == len(self.dataset.sequences[self._seq_id][0]):
                self._seq_id += 1
                self._word_id = 0
            if self._seq_id == len(self.dataset.sequences):
                break
            seq = self.dataset.sequences[self._seq_id][0]
            word = seq[self._word_id]
            lang_id = self.dataset.sequences[self._seq_id][1]
            x.append(word)
            l.append(lang_id)
            y_t = []
            # positive examples
            for ii in range(self._word_id - 2, self._word_id + 3):
                if ii != self._word_id:
                    if ii >= 0 and ii < len(seq):
                        y_t.append(seq[ii])
            y_pos.append(y_t)
            # negative examples
            y_t2 = []
            while len(y_t2) < 4:
                index = random.randint(0, len(self._word_list) - 1)
                word = self._word_list[index]
                if word not in y_t:
                    y_t2.append(word)
            y_neg.append(y_t2)

        return x, y_pos, y_neg, l


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
        x, y_pos, y_neg, l = sdev.get_next_batch(batch_size=batch_size)
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
        tmp = torch.log(1 + torch.exp(-tmp))
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
        tmp = torch.log(1 + torch.exp(tmp))
        loss_neg = tmp.mean()

        loss = loss_pos + loss_neg
        total_loss += loss.item()

    return total_loss / num_batches


def do_train(params):
    ds_list = json.load(open(params.train_file))
    train_list = []
    dev_list = []
    cluster_list = []
    for ii in range(len(ds_list)):
        train_list.append(ds_list[ii][1])
        dev_list.append(ds_list[ii][2])
        cluster_list.append(ds_list[ii][3])

    trainset = Dataset()
    devset = Dataset()
    sys.stdout.write('STEP 1: Loading data\n')
    for ii, train, dev in zip(range(len(train_list)), train_list, dev_list):
        sys.stdout.write('\tLoading language {0}\n'.format(ii))
        trainset.load_dataset(train_list[ii], ii)
        devset.load_dataset(dev_list[ii], ii)
        trainset.load_clusters(cluster_list[ii], ii)

    sys.stdout.write('STEP 2: Computing encodings\n')
    encodings = Encodings()
    encodings.compute_encodings(trainset)
    print(encodings)

    strain = SkipgramDataset(trainset, encodings)
    sdev = SkipgramDataset(devset, encodings)

    model = WordGram(encodings)
    if params.device != 'cpu':
        model.to(params.device)

    import torch.optim as optim
    import torch.nn as nn
    optimizer = optim.Adam(model.parameters(), lr=1e-4)  # , betas=(0.9, 0.9))
    criterion = nn.CrossEntropyLoss()

    patience_left = params.patience
    epoch = 1

    best_nll = 9999
    encodings.save('{0}.encodings'.format(params.store))
    # nll = _eval(model, sdev, criterion, params.batch_size)
    while patience_left > 0:
        patience_left -= 1
        sys.stdout.write('\n\nStarting epoch ' + str(epoch) + '\n')
        sys.stdout.flush()
        random.shuffle(trainset.sequences)
        num_batches = strain.get_count() // params.batch_size
        if strain.get_count() % params.batch_size != 0:
            num_batches += 1
        epoch_loss = 0
        import tqdm
        model.train()
        strain.shuffle()
        pgb = tqdm.tqdm(range(num_batches), desc='\tloss=NaN h=N/A w=N/A', ncols=160)
        for batch_idx in pgb:
            x, y_pos, y_neg, l = strain.get_next_batch(batch_size=params.batch_size)
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
            tmp = torch.log(1 + torch.exp(-tmp))
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
            tmp = torch.log(1 + torch.exp(tmp))
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
