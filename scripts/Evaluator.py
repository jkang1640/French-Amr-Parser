from typing import List, Dict, Tuple
from settings import mbart_lang_code_maps
from tqdm.auto import tqdm
import torch
import os
from pathlib import Path
import os
import numpy as np
from torchmetrics import BLEUScore
from settings import AMR_SCRIPT, CHINESE_VOCAB, CHINESE_VOCAB_PAD
import json
import subprocess
from ucca_to_mrp import UccaMRPConverter
from eval_utils import cd, save_predictions, amr_postprocessing
from delinearize_ucca import UccaParser


class AmrEvaluator:
    def __init__(self, tokenizer,
                 eval_gold_file: Path,
                 sent_path: Path,
                 pred_save_dir: Path,
                 model,
                 dataloader,
                 logger,
                 src_lang="en"):

        self.keep_output_every_n = 100
        self.tokenizer = tokenizer
        self.model = model
        self.dataloader = dataloader
        self.log = logger
        self.model.eval()
        self.eval_gold_file = eval_gold_file
        self.sent_path = sent_path
        self.pred_save_dir = pred_save_dir
        self.src_lang = src_lang

    def gen_outputs(self):
        eval_predictions = []
        decoder_start_token_id = self.tokenizer.convert_tokens_to_ids(["amr"])[0]
        eval_losses = []
        bad_words_ids = None

        if self.src_lang == "zh":
            print("source lang is chinese, applying bad word ids..")
            with open(CHINESE_VOCAB, 'r') as f, open(CHINESE_VOCAB_PAD, 'r') as p:
                # chinese words as a beginning of token ex) ▁了, ▁中, ▁我们
                chinese_tok = [char.rstrip('\n') for char in f.readlines()]
                chinese_tok_ids = self.tokenizer(chinese_tok, add_special_tokens=False).input_ids

                # chinese words as a subtoken ex) ("了", 了中, :了 ...
                chinese_subtok_padded = [char.rstrip('\n') for char in p.readlines()]
                chinese_subtok_padded_ids = self.tokenizer(chinese_subtok_padded, add_special_tokens=False).input_ids
                chinese_subtok_ids = [[bad_word_id[-1]] for bad_word_id in chinese_subtok_padded_ids]
                bad_words_ids = chinese_tok_ids + chinese_subtok_ids

                # remove empty list
                bad_words_ids = [ele for ele in bad_words_ids if ele != []]

        with torch.no_grad():
            for i, dev_input in enumerate(tqdm(self.dataloader)):

                eval_outputs = self.model(**dev_input)
                loss = eval_outputs.loss
                eval_losses.append(loss.item())
                forced_bos_token_id = self.tokenizer.convert_tokens_to_ids(["("])[0]
                eval_generated = self.model.generate(input_ids=dev_input['input_ids'],
                                                     attention_mask=dev_input['attention_mask'],
                                                     decoder_start_token_id=decoder_start_token_id,
                                                     forced_bos_token_id=forced_bos_token_id,
                                                     bad_words_ids=bad_words_ids,
                                                     num_beams=5
                                                     )
                tok_eval_predictions = self.tokenizer.batch_decode(eval_generated, skip_special_tokens=True)
                eval_predictions.extend(tok_eval_predictions)

        return eval_predictions, eval_losses

    @staticmethod
    def compute_smatch(pred_path, ref_path):
        """
        pred_path: path to amr prediction generated by file
        ref_path: path to gold file (required to compute smatch score)  ex: {}.txt.graph
        """
        reformatted = pred_path.parent / (pred_path.name + ".restore.pruned.coref.all.form")
        computed_smatch = AMR_SCRIPT / "smatch" / "f_score.txt"
        # sanity check
        assert ref_path.exists()

        with cd(AMR_SCRIPT):
            subprocess.check_call(
                ["python", "smatch/smatch.py", "-f", reformatted, ref_path, "-r", "5", "--significant", "3"])

        with open(computed_smatch, 'r') as f:
            smatch = float(f.read().strip())

        return smatch

    def run_eval(self, n_step=None):
        self.n_step = n_step
        eval_predictions, eval_losses = self.gen_outputs()

        eval_gold_file = self.eval_gold_file
        eval_pred_file = save_predictions(eval_predictions, self.pred_save_dir / "step_{}".format(self.n_step) / self.src_lang / "pred.txt.tf")
        eval_loss = np.mean(eval_losses)
        sent_path = self.sent_path

        try:
            os.environ["TOKENIZERS_PARALLELISM"] = "false"
            amr_postprocessing(eval_pred_file, sent_path)
            smatch = AmrEvaluator.compute_smatch(eval_pred_file, eval_gold_file)
            os.environ["TOKENIZERS_PARALLELISM"] = "true"

        except subprocess.CalledProcessError as err:
            self.log.info(err)
            smatch = 0

        return eval_loss, smatch

class UccaEvaluator:
    def __init__(self,
                 tokenizer,
                 eval_gold_file: Path,
                 sent_path: Path,
                 pred_save_dir: Path, # should be UCCA_DATA / "Eval"
                 model,
                 dataloader,
                 logger,
                 split="train",
                 src_lang="en"):

        self.keep_output_every_n = 100
        self.tokenizer = tokenizer
        self.model = model
        self.dataloader = dataloader
        self.log = logger
        self.model.eval()
        self.src_lang = src_lang
        self.pred_save_dir = pred_save_dir
        self.split = split
        self.gold_mrp_save_to = pred_save_dir / "{}.gold.mrp".format(self.split)
        self.pred_mrp_save_to = pred_save_dir / "{}.pred.mrp".format(self.split)

        # if structured graph is not restorable from linearized one, use the toy graph and sent for scoring
        self.toy_graph = '[ <root_0> H [ <H_0> D [ <D_0> T [ Highly ] ] S [ <S_0> T [ recommended ] ] ] ]'
        self.toy_sent = "Highly recommended"

        with open(eval_gold_file, 'r') as g, open(sent_path, 'r') as s:
            self.gold_graphs = g.read().splitlines()
            self.sents = s.read().splitlines()

    def save_prediction_raw(self, predictions):
        save_dir = self.pred_save_dir / "step_{}".format(self.n_step)
        save_dir.mkdir(parents=True, exist_ok=True)
        with open(save_dir /'ucca.pred.tf', 'w') as f:
            for pred in predictions:
                f.write(pred)
                f.write("\n")

    def gen_outputs(self):
        eval_predictions = []
        eval_losses = []
        decoder_start_token_id = self.tokenizer.convert_tokens_to_ids(["ucca"])[0]

        with torch.no_grad():
            for i, dev_input in enumerate(tqdm(self.dataloader)):

                eval_outputs = self.model(**dev_input)
                loss = eval_outputs.loss
                eval_losses.append(loss.item())
                forced_bos_token_id = self.tokenizer.convert_tokens_to_ids(["["])[0]
                eval_generated = self.model.generate(input_ids=dev_input['input_ids'],
                                                     attention_mask=dev_input['attention_mask'],
                                                     decoder_start_token_id=decoder_start_token_id,
                                                     forced_bos_token_id=forced_bos_token_id,
                                                     num_beams=5
                                                    )

                gen_tok = self.tokenizer.batch_decode(eval_generated, skip_special_tokens=True)
                eval_predictions.extend(gen_tok)

        self.save_prediction_raw(eval_predictions)
        return eval_predictions, eval_losses

    def delinearize_to_tree(self, graphs, sents):
        # get restored ucca graph
        trees = []
        for i, (graphs, sent) in enumerate(zip(graphs, sents)):
            try:
                parser = UccaParser()
                tree = parser.parse_ucca(graphs)
            except:
                # when cannot restore the tree, replace it with a model tree
                parser = UccaParser()
                tree = parser.parse_ucca(self.toy_graph)
            trees.append(tree)

        return trees

    def reformat_to_mrp(self, trees, sents):
        # reformat the restored graph to mrp format
        mrps = []
        for i, (tree, sent) in enumerate(zip(trees, sents)):
            converter = UccaMRPConverter(tree, sent, i)
            converted = converter.convert_tree_to_json()
            mrps.append(converted)

        return mrps

    def write_mrp(self, save_to, data):
        with open(save_to, 'w') as f:
            for item in data:
                json.dump(item, f)
                f.write("\n")

    @staticmethod
    def compute_ucca(gold_file, pred_file):

        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        output = subprocess.check_output(["mtool", "--score", "ucca", "--read", "mrp", "--gold", gold_file, pred_file])
        ucca_report = json.loads(output)
        ucca_f1 = ucca_report['labeled']['primary']['f']
        os.environ["TOKENIZERS_PARALLELISM"] = "true"

        return round(ucca_f1, 3)

    def run_eval(self, n_step=None):
        self.n_step = n_step
        predictions, losses = self.gen_outputs()
        loss = np.mean(losses)
        gold_trees = self.delinearize_to_tree(self.gold_graphs, self.sents)
        pred_trees = self.delinearize_to_tree(predictions, self.sents)

        gold_mrp_format = self.reformat_to_mrp(gold_trees, self.sents)
        pred_mrp_format = self.reformat_to_mrp(pred_trees, self.sents)

        self.write_mrp(self.gold_mrp_save_to, gold_mrp_format)
        self.write_mrp(self.pred_mrp_save_to, pred_mrp_format)

        ucca_f1 = self.compute_ucca(self.gold_mrp_save_to, self.pred_mrp_save_to)

        return loss, ucca_f1

class MTEvaluator:
    def __init__(self, tokenizer, model, pred_save_dir, mt_dataloaders : Dict[str, str], log):
        self.keep_output_every_n = 100
        self.tokenizer = tokenizer
        self.tgt_lang = mbart_lang_code_maps["en"]
        self.model = model
        self.mt_dataloaders = mt_dataloaders
        self.log = log
        self.pred_save_dir = pred_save_dir

    def run_eval(self, n_step=None):
        self.n_step = n_step
        bleu_scores_all_langs = []

        for src_lang in self.mt_dataloaders:
            bleu = self.eval_translation(src_lang, self.tokenizer, self.model, self.mt_dataloaders[src_lang])
            bleu_scores_all_langs.append(bleu.item())
            self.log.info("bleu score for {}->{}:{}:".format(src_lang, self.tgt_lang[:2], bleu))


        return sum(bleu_scores_all_langs) / len(bleu_scores_all_langs)

    def eval_translation(self, src_lang, tokenizer, model, dataloader):

        translations, targets = self.gen_translations_n_targets(src_lang, dataloader, tokenizer, model)

        return self.get_bleu(translations, targets)

    @staticmethod
    def get_bleu(preds, targets) -> torch.Tensor:

        metric = BLEUScore()
        bleu_scores_all_sents = []

        for pred, target in zip(preds,targets):
            score = metric(preds=[pred], target=[[target]])
            bleu_scores_all_sents.append(score)

        mean = torch.mean(torch.stack(bleu_scores_all_sents))

        return mean

    def gen_translations_n_targets(self, src_lang, dataloader, tokenizer, model) -> Tuple[List, List]:

        decoder_start_token_id = tokenizer.convert_tokens_to_ids([self.tgt_lang])[0]
        pred_translations = []
        gold_translations = []

        with torch.no_grad():
            for i, dev_input in enumerate(tqdm(dataloader)):
                predictions = model.generate(input_ids=dev_input['input_ids'],
                                             attention_mask=dev_input['attention_mask'],
                                             decoder_start_token_id=decoder_start_token_id,
                                             num_beams=5
                                             )
                mt_translations = tokenizer.batch_decode(predictions, skip_special_tokens=True)
                pred_translations.extend(mt_translations)
                targets = tokenizer.batch_decode(dev_input['labels'], skip_special_tokens=True)
                gold_translations.extend(targets)

        save_to = self.pred_save_dir / "step_{}".format(self.n_step) / "{}-{}.{}".format(self.tgt_lang[:2], src_lang, self.tgt_lang)
        save_predictions(pred_translations, save_to)

        return pred_translations, gold_translations