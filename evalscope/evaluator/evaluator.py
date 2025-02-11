# Copyright (c) Alibaba, Inc. and its affiliates.

import json
import os
import time
from collections import OrderedDict
from copy import deepcopy
from tqdm import tqdm
from typing import Any, Dict, List, Optional, Union

from evalscope.benchmarks import DataAdapter
from evalscope.config import TaskConfig
from evalscope.constants import (DEFAULT_DATASET_CACHE_DIR, AnswerKeys, DumpMode, EvalStage, EvalType, HubType,
                                 ReviewKeys)
from evalscope.models.model_adapter import BaseModelAdapter, CustomModelAdapter
from evalscope.tools.combine_reports import gen_table
from evalscope.utils import dict_torch_dtype_to_str, gen_hash
from evalscope.utils.io_utils import OutputsStructure, dump_jsonl_data, jsonl_to_list
from evalscope.utils.logger import get_logger

logger = get_logger()


class Evaluator(object):
    """
    The evaluator for model on datasets.

    Args:
        dataset_name_or_path: str, the dataset name or path.
                if the dataset is a local path, e.g. /path/to/your_dataset_name,
                then the task name will be the basename of the path, which is `your_dataset_name`.
        data_adapter: DataAdapter, the data adapter for the dataset.
        subset_list: list, the subset list for the dataset.
        model_adapter: BaseModelAdapter, the model adapter for the model.
        use_cache: str, path to local cache. Default: None
        outputs_dir: OutputsStructure, the outputs dir. Default: None
        datasets_dir: str, the datasets dir. Default: DEFAULT_ROOT_CACHE_DIR
        datasets_hub: str, the datasets hub. `Local`, `ModelScope` or `HuggingFace`. Default: 'ModelScope'
        stage: str, the stage of evaluation. `all` or `infer` or `review`. Default: 'all'
        eval_type: str, the evaluation type. `checkpoint` or `service` or `custom`. Default: 'checkpoint'
        overall_task_cfg: dict, the overall task config. Default: None
        **kwargs: kwargs.
    """

    def __init__(self,
                 dataset_name_or_path: str,
                 data_adapter: DataAdapter,
                 subset_list: Optional[list] = None,
                 model_adapter: Optional[BaseModelAdapter] = None,
                 use_cache: Optional[str] = None,
                 outputs: Optional[OutputsStructure] = None,
                 datasets_dir: Optional[str] = DEFAULT_DATASET_CACHE_DIR,
                 datasets_hub: Optional[str] = HubType.MODELSCOPE,
                 stage: Optional[str] = EvalStage.ALL,
                 eval_type: Optional[str] = EvalType.CHECKPOINT,
                 overall_task_cfg: Optional[TaskConfig] = None,
                 **kwargs):

        self.dataset_name_or_path = os.path.expanduser(dataset_name_or_path)
        self.dataset_name = os.path.basename(self.dataset_name_or_path.rstrip(os.sep)).split('.')[0]
        self.model_name = overall_task_cfg.model_id
        self.custom_task_name = f'{self.model_name}_{self.dataset_name}'

        self.datasets_dir = os.path.expanduser(datasets_dir)
        self.kwargs = kwargs
        self.data_adapter = data_adapter
        self.model_adapter = model_adapter
        self.eval_type = eval_type
        self.stage = stage
        self.use_cache = use_cache
        self.overall_task_cfg = overall_task_cfg
        if isinstance(self.model_adapter, CustomModelAdapter):
            self.overall_task_cfg.model_args = self.model_adapter.custom_model.config

        self.model_cfg = self.model_adapter.model_cfg

        # Deal with the output paths
        self.outputs_structure = outputs

        # Load dataset
        self.dataset = self.data_adapter.load(
            dataset_name_or_path=dataset_name_or_path,
            subset_list=subset_list,
            work_dir=self.datasets_dir,
            datasets_hub=datasets_hub,
            **kwargs)

        # Get prompts from dataset
        # TODO: support sampler
        self.prompts = self.data_adapter.gen_prompts(data_dict=self.dataset)
        del self.dataset

    def _pred_answer(self, input_d: dict, infer_cfg: dict, subset_name: str, answer_id: str = None) -> dict:

        ans: dict = self.model_adapter.predict(inputs=input_d, infer_cfg=infer_cfg)
        ans[AnswerKeys.ANSWER_ID] = answer_id
        ans[AnswerKeys.SUBSET_NAME] = subset_name

        return ans

    def get_answers(self,
                    subset_name: str,
                    prompts_list: List[dict],
                    infer_cfg: dict = None,
                    debug: bool = False,
                    **kwargs) -> list:
        """
        Get answers from model inference.
        It is required to rewrite this method to support your own evaluator.

        Args:
            subset_name: subset name for benchmark.
            prompts_list: prompts list.
            infer_cfg: model inference config.
                Attributes:
                    do_sample: bool, whether to use sampling.
                    top_k: int, the number of highest probability vocabulary tokens to keep for top-k-filtering.
                    top_p: float, if set to float < 1, only the most probable tokens with probabilities to add.
                    temperature: float, the value used to module the next token probabilities.
                    num_beams: int, number of beams for beam search. 1 means no beam search.
                    max_length: int, the max length of the sequence to be generated.
                    max_new_tokens: int, the max number of new tokens to be generated.
                    repetition_penalty: float, the parameter for repetition penalty. 1.0 means no penalty.
            debug: whether to run in debug mode.
            **kwargs: kwargs.

        Returns: The list of answers.
        """
        assert self.data_adapter is not None, 'data_adapter must be provided when calling func get_answers() !'
        assert self.model_adapter is not None, 'model must be provided when calling func get_answers() !'
        assert len(prompts_list) > 0, 'prompts_list must not be empty when calling func get_answers() !'

        answers_list = []
        pred_file_name = self.dataset_name + '_' + subset_name + '.jsonl'
        pred_file_path = os.path.join(self.outputs_structure.predictions_dir, self.model_name, pred_file_name)
        os.makedirs(os.path.dirname(pred_file_path), exist_ok=True)

        if self.use_cache and os.path.exists(pred_file_path):
            answers_list = jsonl_to_list(pred_file_path)
            logger.info(f'Reusing predictions from {pred_file_path}, got {len(answers_list)} answers.')
            # Note: assume prediction in order of prompts_list
            prompts_list = prompts_list[len(answers_list):]

        if isinstance(self.model_adapter, CustomModelAdapter):
            # Batch inference for custom model

            resp_answers_list: List[Dict[str, Any]] = self.model_adapter.predict(
                inputs=prompts_list, infer_cfg=infer_cfg)

            assert len(prompts_list) == len(resp_answers_list), \
                f'Length of prompts_list({len(prompts_list)}) != Length of resp_answers_list({len(resp_answers_list)})'

            for in_d, resp_d in zip(prompts_list, resp_answers_list):

                # Gen answer_id (concat: model_cfg + input_prompt + infer_cfg)
                model_cfg_str = json.dumps(
                    OrderedDict(sorted(dict_torch_dtype_to_str(self.model_adapter.model_cfg).items())),
                    ensure_ascii=False)
                input_prompt_str = json.dumps(
                    OrderedDict(sorted(dict_torch_dtype_to_str(in_d).items())), ensure_ascii=False)
                infer_cfg_str = json.dumps(
                    OrderedDict(sorted(dict_torch_dtype_to_str(infer_cfg).items())), ensure_ascii=False)
                answer_id = 'answer-' + gen_hash(model_cfg_str + input_prompt_str + infer_cfg_str)

                resp_d[AnswerKeys.MODEL_SPEC] = self.model_adapter.model_cfg
                resp_d[AnswerKeys.ANSWER_ID] = answer_id
                resp_d[AnswerKeys.SUBSET_NAME] = subset_name
                resp_d[AnswerKeys.RAW_INPUT] = in_d[AnswerKeys.RAW_INPUT]
                resp_d[AnswerKeys.ORIGIN_PROMPT] = in_d

                answers_list.append(resp_d)
                dump_jsonl_data(resp_d, pred_file_path, dump_mode=DumpMode.APPEND)

        else:
            for input_prompt in tqdm(prompts_list, total=len(prompts_list), desc=f'Predicting({subset_name}): '):

                # Gen answer_id (concat: model_cfg + input_prompt + infer_cfg)
                model_cfg_str = json.dumps(
                    OrderedDict(sorted(dict_torch_dtype_to_str(self.model_adapter.model_cfg).items())),
                    ensure_ascii=False)
                input_prompt_str = json.dumps(
                    OrderedDict(sorted(dict_torch_dtype_to_str(input_prompt).items())), ensure_ascii=False)
                infer_cfg_str = json.dumps(
                    OrderedDict(sorted(dict_torch_dtype_to_str(infer_cfg).items())), ensure_ascii=False)
                answer_id = 'answer-' + gen_hash(model_cfg_str + input_prompt_str + infer_cfg_str)

                # Get answers
                answer_d: dict = self._pred_answer(
                    input_d=input_prompt, infer_cfg=infer_cfg, subset_name=subset_name, answer_id=answer_id)

                answer_d[AnswerKeys.MODEL_SPEC] = self.model_adapter.model_cfg
                answer_d[AnswerKeys.RAW_INPUT] = input_prompt[AnswerKeys.RAW_INPUT]
                answer_d[AnswerKeys.ORIGIN_PROMPT] = input_prompt

                if debug:
                    logger.info(f'**input_prompt: {json.dumps(input_prompt, ensure_ascii=False)} \n')
                    logger.info(f'**predicted ans: {json.dumps(answer_d, ensure_ascii=False)} \n')

                answers_list.append(answer_d)
                dump_jsonl_data(answer_d, pred_file_path, dump_mode=DumpMode.APPEND)

        logger.info(f'Dump predictions to {pred_file_path}.')
        return answers_list

    def _get_review(self, answer_d: dict, review_id: str = None, reviewer_spec: dict = None) -> dict:

        if reviewer_spec is None:
            reviewer_spec = {}

        review_res = deepcopy(answer_d)
        choices = review_res[AnswerKeys.CHOICES]
        if len(choices) == 0:
            review_res[ReviewKeys.REVIEWED] = False
            review_res[ReviewKeys.REVIEW_ID] = None
            review_res[ReviewKeys.REVIEWER_SPEC] = reviewer_spec
            review_res[ReviewKeys.REVIEW_TIME] = time.time()
            return review_res

        rev_choices = []
        for choice in choices:
            raw_input_d: dict = review_res[AnswerKeys.RAW_INPUT]
            answer_content = choice[ReviewKeys.MESSAGE][ReviewKeys.CONTENT]
            answer_content = self.data_adapter.parse_pred_result(
                result=answer_content, raw_input_d=raw_input_d, eval_type=self.eval_type)
            gold_content = self.data_adapter.get_gold_answer(raw_input_d)

            review_result = self.data_adapter.match(gold_content, answer_content)
            choice[ReviewKeys.REVIEW] = {
                ReviewKeys.GOLD: gold_content,
                ReviewKeys.PRED: answer_content,
                ReviewKeys.RESULT: review_result
            }

            rev_choices.append(choice)

        review_res[AnswerKeys.CHOICES] = rev_choices
        review_res[ReviewKeys.REVIEWED] = True
        review_res[ReviewKeys.REVIEW_ID] = review_id
        review_res[ReviewKeys.REVIEWER_SPEC] = reviewer_spec
        review_res[ReviewKeys.REVIEW_TIME] = time.time()

        return review_res

    def get_reviews(self, subset_name: str, answers_list: List[dict], debug: bool = False, **kwargs) -> list:
        """
        Get reviews from answers.
        It is required to rewrite this method to support your own evaluator.

        Args:
            subset_name: subset name of benchmark
            answers_list: inference results list.
            debug: whether to run in debug mode.
            **kwargs: kwargs.

        Returns: reviews list.
        """
        reviews_list = []

        review_file_name = self.dataset_name + '_' + subset_name + '.jsonl'
        review_file_path = os.path.join(self.outputs_structure.reviews_dir, self.model_name, review_file_name)
        os.makedirs(os.path.dirname(review_file_path), exist_ok=True)

        if self.use_cache and os.path.exists(review_file_path):
            logger.warning(f'Ignore use_cache={self.use_cache}, updating the review file: {review_file_path} ...')

        for answer_d in tqdm(answers_list, total=len(answers_list), desc=f'Reviewing({subset_name}): '):

            # Gen review_id (concat: answer_id + reviewer_spec)
            answer_id = answer_d[AnswerKeys.ANSWER_ID]

            reviewer_spec: dict = {
                'metric': [metric_d['name'] for metric_d in self.data_adapter.metric_list],
                'reviewer': ['Evaluator'],
                'revision': ['default']
            }
            reviewer_spec_str = json.dumps(
                OrderedDict(sorted(dict_torch_dtype_to_str(reviewer_spec).items())), ensure_ascii=False)
            review_id = 'review-' + gen_hash(answer_id + reviewer_spec_str)

            # Get review
            review_d = self._get_review(answer_d=answer_d, review_id=review_id, reviewer_spec=reviewer_spec)

            if debug:
                logger.info(review_d)

            reviews_list.append(review_d)

            # Dump reviews
            dump_jsonl_data(review_d, review_file_path, dump_mode=DumpMode.APPEND)

        return reviews_list

    def compute_metrics(self, reviews_list: List[dict]) -> Any:
        """
        To compute metrics from reviews_list for each subset.
        It is required to rewrite this method to support your own evaluator.

        Args:
            reviews_list: reviews list.

        Returns:
            The metric result. Depends on the metric function in data_adapter.
        """

        review_res_list = []
        for review_d in reviews_list:
            if not review_d[ReviewKeys.REVIEWED]:
                logger.warning(f'Review not finished for answer_id: {review_d[AnswerKeys.ANSWER_ID]}')
                continue

            review_res = review_d[AnswerKeys.CHOICES][0][ReviewKeys.REVIEW][ReviewKeys.RESULT]
            review_res_list.append(review_res)

        metric_score: Union[float, dict] = self.data_adapter.compute_metric(review_res_list=review_res_list)

        return metric_score

    def dump_report(self, reviews_score_all: dict, use_table: bool = True):
        """
        Get report for total reviews of specific dataset.
        It is required to rewrite this method to support your own evaluator.

        Args:
            report_map: report dict. Generated by func self.data_adapter.gen_report().
            use_table: whether to generate table for reports. Default to True.

        Returns: None
        """
        # Get report map
        report_map: dict = self.data_adapter.gen_report(
            subset_score_map=reviews_score_all, report_name=self.custom_task_name)
        report_map.update(dict(model_name=self.model_name, dataset_name=self.dataset_name))

        # Dump report
        report_path: str = os.path.join(self.outputs_structure.reports_dir, self.model_name,
                                        self.dataset_name + '.json')
        os.makedirs(os.path.dirname(report_path), exist_ok=True)

        # Write report
        with open(report_path, 'w') as f:
            f.write(json.dumps(report_map, ensure_ascii=False, indent=4))
        logger.info(f'Dump report: {report_path} \n')

        # Make table
        if use_table:
            try:
                report_table: str = gen_table([self.outputs_structure.reports_dir])
                logger.info(f'Report table: \n{report_table} \n')
            except Exception:
                logger.error('Failed to generate report table.')
        return report_map

    def eval(self, infer_cfg: dict = None, debug: bool = False, **kwargs) -> dict:
        """
        Evaluate the model on the specific benchmark. Streaming & parallel mode is supported.
        It is required to rewrite this method to support your own evaluator.

        The evaluation process is as follows:
            1. Get the input samples from the dataset (benchmarks on the ModelScope or HuggingFace).
            2. Get the input prompts from dataset with specific data adapter.
            3. Get answers with model inference.
            4. Get reviews with metric function (or reviewers).
            5. Generate report from review results.

        Args:
            infer_cfg: The config for model inference.
            debug: Whether to run in debug mode. Default: False.

        Returns:
            Dict of results. Depends on the stage of evaluation.

            stage == 'all': return the report_map
            stage == 'infer': return the answers_map
            stage == 'review': return the reviews_map
        """

        logger.info(f'**** Start evaluating on dataset {self.dataset_name_or_path} ****')

        reviews_score_all = {}  # {subset_name: (score, num)}
        stage_answers_dict = {}
        stage_reviews_dict = {}

        for subset_name, prompts_list in self.prompts.items():
            limit = kwargs.get('limit', len(prompts_list))
            prompts_list = prompts_list[:limit]

            answers_list: list = self.get_answers(
                subset_name=subset_name, prompts_list=prompts_list, infer_cfg=infer_cfg, debug=debug, **kwargs)
            if self.stage == EvalStage.INFER:
                stage_answers_dict[subset_name] = answers_list
                continue

            reviews_list: list = self.get_reviews(
                subset_name=subset_name, answers_list=answers_list, debug=debug, **kwargs)

            metric_res = self.compute_metrics(reviews_list=reviews_list)
            reviews_score_all[subset_name] = (metric_res, len(reviews_list))
            stage_reviews_dict[subset_name] = reviews_list

        if self.stage == EvalStage.INFER:
            return stage_answers_dict

        if self.stage == EvalStage.REVIEW:
            return stage_reviews_dict

        # Generate report
        report_map = self.dump_report(reviews_score_all)

        logger.info(f'**** Evaluation finished on {self.dataset_name_or_path} ****\n')

        return report_map
