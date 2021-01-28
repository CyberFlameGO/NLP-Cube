import os, sys, yaml

sys.path.append(".")
from audioop import add

from pytorch_lightning.callbacks import EarlyStopping

from argparse import ArgumentParser

from torch.utils.data import DataLoader
import pytorch_lightning as pl

from cube3.io_utils.config import TaggerConfig, ParserConfig, TokenizerConfig, LemmatizerConfig
from cube3.io_utils.encodings import Encodings
from cube3.io_utils.objects import Document
from cube3.networks.tokenizer import Tokenizer
from cube3.networks.tagger import Tagger
from cube3.networks.parser import Parser
from cube3.networks.lemmatizer import Lemmatizer
from cube3.networks.utils import LMHelper, MorphoDataset, MorphoCollate, TokenizationDataset, TokenCollate, \
    Word2TargetCollate, LemmaDataset, CompoundDataset


class Trainer():
    def __init__(self, task: str, language_map: {}, language_codes: [], train_files: {}, dev_files: {}, test_files: {},
                 args):
        self.task = None
        if task not in ["tokenizer", "lemmatizer", "cwe", "tagger", "parser"]:
            raise Exception("Task must be one of: tokenizer, lemmatizer, cwe, tagger or parser.")

        self.store_prefix = args.store
        self.language_map = language_map
        self.language_codes = language_codes
        self.args = args

        # TODO assert lang_id matches train
        # TODO assert train files code are found in dev files
        # lang_code_list = sorted(list(set(code for code in train_files )))

        self.task = task
        self.doc_train = Document()
        self.doc_dev = Document()
        self.doc_test = Document()

        for lang_code in train_files:
            print("Reading train file for language code {} : {}".format(lang_code, train_files[lang_code]))
            self.doc_train.load(train_files[lang_code], lang_id=language_codes.index(lang_code))
        for lang_code in dev_files:
            print("Reading dev file for language code {} : {}".format(lang_code, dev_files[lang_code]))
            self.doc_dev.load(dev_files[lang_code], lang_id=language_codes.index(lang_code))
        for lang_code in test_files:
            print("Reading test file for language code {} : {}".format(lang_code, test_files[lang_code]))
            self.doc_test.load(test_files[lang_code], lang_id=language_codes.index(lang_code))

        # ensure store dir exists
        i = self.store_prefix.rfind("/")
        if i > 0:
            if i >= len(self.store_prefix) - 1:
                raise Exception(
                    "store_prefix is a folder; please specify the prefix of the models after the '/', like 'data/tagger'.")

            target_folder = self.store_prefix[:i]
            model_prefix = self.store_prefix[i + 1:]
            os.makedirs(target_folder, exist_ok=True)
            print("Saving model in {}, with prefix {}".format(target_folder, model_prefix))
        else:
            print("Saving model in the current folder, with prefix {}".format(self.store_prefix))

    def fit(self):
        if self.task not in ["tokenizer", "lemmatizer", "cwe", "tagger", "parser"]:
            raise Exception("Task must be one of: tokenizer, lemmatizer, cwe, tagger or parser.")

        with open(self.args.store + ".yaml", 'w') as f:
            yaml.dump({"language_map": self.language_map, "language_codes": self.language_codes}, f, sort_keys=True)

        enc = Encodings()
        enc.compute(self.doc_train, None)
        enc.save('{0}.encodings'.format(self.store_prefix))

        if self.task == "tokenizer":
            config = TokenizerConfig()
            no_space_lang = Tokenizer._detect_no_space_lang(self.doc_train)
            print("NO_SPACE_LANG = " + str(no_space_lang))
            config.no_space_lang = no_space_lang
        if self.task == "tagger":
            config = TaggerConfig()
        if self.task == "lemmatizer":
            config = LemmatizerConfig()
        if self.task == "parser":
            config = ParserConfig()
        config.lm_model = self.args.lm_model
        if self.args.config_file:
            config.load(self.args.config_file)
            if self.args.lm_model is not None:
                config.lm_model = self.args.lm_model
        config.save('{}.config'.format(self.args.store))

        if self.task != "tokenizer" and self.task != 'lemmatizer' and self.task != 'cwe':
            helper = LMHelper(device=self.args.lm_device, model=config.lm_model)
            helper.apply(self.doc_dev)
            helper.apply(self.doc_train)

        if self.task == "tokenizer":
            trainset = TokenizationDataset(self.doc_train)
            devset = TokenizationDataset(self.doc_dev, shuffle=False)
        elif self.task == 'parser' or self.task == 'tagger':
            trainset = MorphoDataset(self.doc_train)
            devset = MorphoDataset(self.doc_dev)
        elif self.task == 'lemmatizer':
            trainset = LemmaDataset(self.doc_train)
            devset = LemmaDataset(self.doc_dev)

        collate = MorphoCollate(enc)

        # per task specific settings
        callbacks = []
        if self.task == "tokenizer":
            early_stopping_callback = EarlyStopping(
                monitor='val/early_meta',
                patience=args.patience,
                verbose=True,
                mode='max'
            )
            collate = TokenCollate(enc, lm_device=args.lm_device, lm_model=args.lm_model,
                                   no_space_lang=config.no_space_lang)
            callbacks = [early_stopping_callback, Tokenizer.PrintAndSaveCallback(self.store_prefix)]
            model = Tokenizer(config=config, encodings=enc, language_codes=self.language_codes)

        if self.task == "tagger":
            early_stopping_callback = EarlyStopping(
                monitor='val/early_meta',
                patience=args.patience,
                verbose=True,
                mode='max'
            )
            callbacks = [early_stopping_callback, Tagger.PrintAndSaveCallback(self.store_prefix)]
            model = Tagger(config=config, encodings=enc, language_codes=self.language_codes)

        if self.task == "parser":
            collate = MorphoCollate(enc, add_parsing=True)
            early_stopping_callback = EarlyStopping(
                monitor='val/early_meta',
                patience=args.patience,
                verbose=True,
                mode='max'
            )
            callbacks = [early_stopping_callback, Parser.PrintAndSaveCallback(self.store_prefix)]
            model = Parser(config=config, encodings=enc, language_codes=self.language_codes)

        if self.task == "lemmatizer":
            collate = Word2TargetCollate(enc)
            early_stopping_callback = EarlyStopping(
                monitor='val/early_meta',
                patience=args.patience,
                verbose=True,
                mode='max'
            )
            callbacks = [early_stopping_callback, Lemmatizer.PrintAndSaveCallback(self.store_prefix)]
            model = Lemmatizer(config=config, encodings=enc, language_codes=self.language_codes)

        # dataloaders
        train_loader = DataLoader(trainset, batch_size=self.args.batch_size, collate_fn=collate.collate_fn,
                                  shuffle=True,
                                  num_workers=self.args.num_workers)
        val_loader = DataLoader(devset, batch_size=self.args.batch_size, collate_fn=collate.collate_fn,
                                num_workers=self.args.num_workers)

        # pre-train checks
        resume_from_checkpoint = None
        if self.args.resume is True:
            resume_from_checkpoint = self.store_prefix + ".last"
            if not os.path.exists(resume_from_checkpoint):
                raise Exception("Resume from checkpoint: {} not found!".format(resume_from_checkpoint))

        if self.args.gpus == 0:
            acc = 'ddp_cpu'
        else:
            acc = 'ddp'

        trainer = pl.Trainer(
            gpus=args.gpus,
            accelerator=acc,
            num_nodes=1,
            default_root_dir='data/',
            callbacks=callbacks,
            resume_from_checkpoint=resume_from_checkpoint,
            accumulate_grad_batches=args.grad_acc,
            # limit_train_batches=100,
            # limit_val_batches=4,
        )

        # run fit
        print("\nStarting train\n")
        trainer.fit(model, train_loader, val_loader)


if __name__ == '__main__':
    parser = ArgumentParser(description='NLP-Cube Trainer Helper')
    parser.add_argument('--task', action='store', dest='task',
                        help='Type of task : "tokenizer", "lemmatizer", "cwe", "tagger", "parser"')
    parser.add_argument('--train', action='store', dest='train_file',
                        help='Start building a tagger model')
    parser.add_argument('--patience', action='store', type=int, default=20, dest='patience',
                        help='Number of epochs before early stopping (default=20)')
    parser.add_argument('--store', action='store', dest='store', help='Output base', default='data/model')
    parser.add_argument('--gpus', action='store', dest='gpus', type=int,
                        help='How many GPUs to use (default=1)', default=1)
    parser.add_argument('--num-workers', action='store', dest='num_workers', type=int,
                        help='How many dataloaders to use (default=4)', default=4)
    parser.add_argument('--accelerator', action='store', type=str, default="ddp", dest='accelerator',
                        help='Accelerator (see Pytorch Lightning accelerators)')
    parser.add_argument('--batch-size', action='store', type=int, default=16, dest='batch_size',
                        help='Batch size (default=16)')
    parser.add_argument('--grad-acc', action='store', type=int, default=1, dest='grad_acc',
                        help='Gradient accumulation steps (default=1)')
    parser.add_argument('--debug', action='store_true', dest='debug',
                        help='Do some standard stuff to debug the model')
    parser.add_argument('--resume', action='store_true', dest='resume', help='Resume training')
    parser.add_argument('--lm-model', action='store', dest='lm_model',
                        help='What LM model to use (default=xlm-roberta-base)')
    parser.add_argument('--lm-device', action='store', dest='lm_device', default='cuda:0',
                        help='Where to load LM (default=cuda:0)')
    parser.add_argument('--config', action='store', dest='config_file', help='Load config file')

    args = parser.parse_args()

    with open(args.train_file) as file:
        train_config = yaml.full_load(file)

    trainer_object = Trainer(
        task=args.task,
        language_map=train_config["language_map"],
        language_codes=train_config["language_codes"],
        train_files=train_config["train_files"],
        dev_files=train_config["dev_files"],
        test_files=train_config["test_files"],
        args=args,
    )

    trainer_object.fit()
