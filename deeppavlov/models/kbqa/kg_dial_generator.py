# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import itertools
import math
import time
from logging import getLogger
from typing import List

import torch
from transformers import AutoTokenizer, AutoModelWithLMHead
from deeppavlov.core.common.registry import register
from deeppavlov.core.models.component import Component
from deeppavlov.core.commands.utils import expand_path
from deeppavlov.core.common.file import read_json
from deeppavlov.models.kbqa.rel_ranking_infer import RelRankerInfer
from deeppavlov.models.kbqa.wiki_parser import WikiParser

log = getLogger(__name__)


@register('kg_dial_generator')
class KGDialGenerator(Component):
    def __init__(self, transformer_model: str,
                       path_to_model: str,
                       conf_thres: float = 0.3, *args, **kwargs) -> None:
                       
        self.tokenizer = AutoTokenizer.from_pretrained(transformer_model)
        special_tokens_dict = {"sep_token": "<SEP>"}
        self.tokenizer.add_special_tokens(special_tokens_dict)
        self.model = AutoModelWithLMHead.from_pretrained(str(expand_path(path_to_model)))
        self.model.resize_token_embeddings(len(self.tokenizer))
        self.device = torch.device("cuda")
        self.model.to(self.device)
        self.conf_thres = conf_thres
        
    def __call__(self, prev_utterances_batch: List[str], triplets_batch: List[List[str]],
                       conf_batch: List[float]) -> List[str]:
        tm_st = time.time()
        generated_utterances_batch = []
        for prev_utterance, triplets, conf in zip(prev_utterances_batch, triplets_batch, conf_batch):
            log.debug(f"prev_utterance {prev_utterance} triplets {triplets}")
            if conf > self.conf_thres:
                if triplets:
                    triplets = ' '.join(triplets)
                    context_plus_gk = triplets + " <SEP> " + prev_utterance + self.tokenizer.eos_token
                else:
                    context_plus_gk = prev_utterance + self.tokenizer.eos_token
                log.debug(f"context and gk: {context_plus_gk}")
                input_ids = self.tokenizer.encode(context_plus_gk, return_tensors="pt").to(self.device)
                generated_ids = self.model.generate(input_ids, max_length=200,
                                                    pad_token_id=self.tokenizer.eos_token_id,
                                                    no_repeat_ngram_size=3, do_sample=True,
                                                    top_k=100, top_p=0.7, temperature=0.8)
                generated_utterance = self.tokenizer.decode(generated_ids[:, input_ids.shape[-1]:][0],
                                                            skip_special_tokens=True)
            else:
                generated_utterance = ""
            generated_utterances_batch.append(generated_utterance)
        tm_end = time.time()
        log.info(f"Utterance generation time: {tm_end-tm_st}")
        
        return generated_utterances_batch, conf_batch
        

@register('dial_path_ranker')    
class DialPathRanker(Component):
    def __init__(self, wiki_parser: WikiParser,
                       path_ranker: RelRankerInfer,
                       type_paths_file: str,
                       type_groups_file: str,
                       rel_freq_file: str,
                       max_log_freq: float = 8.0,
                       use_api_requester: bool = False,
                       use_path_stat: bool = False,
                       *args, **kwargs) -> None:
        
        self.type_paths = read_json(expand_path(type_paths_file))
        self.type_groups = read_json(expand_path(type_groups_file))
        self.rel_freq = read_json(expand_path(rel_freq_file))
        
        self.wiki_parser = wiki_parser
        self.path_ranker = path_ranker
        
        self.max_log_freq = max_log_freq
        self.use_api_requester = use_api_requester
        self.use_path_stat = use_path_stat
    
    def __call__(self, utterances_batch: List[str], entities_batch: List[List[str]]) -> List[List[str]]:
        paths_batch = []
        conf_batch = []
        tm_st = time.time()
        for utterance, entities_list in zip(utterances_batch, entities_batch):
            if entities_list:
                entity = entities_list[0]
                if isinstance(entity, list):
                    entity = entity[0]
                log.debug(f"seed entity {entity}")
                entity_types = self.wiki_parser(["find_types"], [entity])[0]
                if self.use_api_requester:
                    entity_types = entity_types[0]
                entity_types = set(entity_types)
                log.debug(f"entity types {entity_types}")
                candidate_paths = set()
                if self.use_path_stat:
                    for entity_type in entity_types:
                        candidate_paths = candidate_paths.union(set([tuple([tuple(path), score])
                            for path, score in self.type_paths.get(entity_type, set())]))
                    candidate_paths = list(candidate_paths)
                    candidate_paths = sorted(candidate_paths, key=lambda x: x[1], reverse=True)
                    candidate_paths = [path for path, score in candidate_paths]
                else:
                    for entity_type in entity_types:
                        candidate_paths = candidate_paths.union(set([tuple(path)
                            for path, score in self.type_paths.get(entity_type, set())]))
                if not candidate_paths:
                    candidate_paths = set()
                    log.debug("not found candidate paths, looking in types dict")
                    add_entity_types = set()
                    subclasses = self.wiki_parser(["find_object" for _ in entity_types],
                                                  [(entity_type, "P279", "forw") for entity_type in entity_types])
                    subclasses = list(itertools.chain.from_iterable(subclasses))
                    for subcls in subclasses:
                        subclass_group = set(self.type_groups.get(subcls, []))
                        if entity_types.intersection(subclass_group):
                            add_entity_types = add_entity_types.union(subclass_groups.difference(entity_types))
                    if self.use_path_stat:
                        for entity_type in entity_types:
                            candidate_paths = candidate_paths.union(set([tuple([tuple(path), score])
                                for path, score in self.type_paths.get(entity_type, set())]))
                        candidate_paths = list(candidate_paths)
                        candidate_paths = sorted(candidate_paths, key=lambda x: x[1], reverse=True)
                        candidate_paths = [path for path, score in candidate_paths]
                    else:
                        for entity_type in entity_types:
                            candidate_paths = candidate_paths.union(set([tuple(path)
                                for path, score in self.type_paths.get(entity_type, set())]))
                        
                candidate_paths = list(candidate_paths)
                log.debug(f"candidate paths {candidate_paths[:10]}")
                
                retrieved_paths = []
                conf = 0.0
                if candidate_paths:
                    if self.use_path_stat:
                        top_paths = candidate_paths
                    else:
                        paths_with_scores = self.path_ranker.rank_paths(utterance, candidate_paths)
                        log.debug(f"paths with scores {paths_with_scores[:10]}")
                        top_paths = [path for path, score in paths_with_scores]
                    log.debug(f"top paths {top_paths[:10]}")
                    tm_wp_st = time.time()
                    wp_res = self.wiki_parser(["retrieve_paths"], [[entity, top_paths]])[0]
                    tm_wp_end = time.time()
                    log.info(f"wiki_parser time: {tm_wp_end-tm_wp_st}")
                    if self.use_api_requester:
                        wp_res = wp_res[0]
                    retrieved_paths, retrieved_rels = wp_res
                    log.info(f"retrieved paths {retrieved_paths}")
                    if retrieved_paths:
                        chosen_path = retrieved_paths[0]
                        chosen_rels = retrieved_rels[0]
                        conf = min(math.log(sum([self.rel_freq.get(rel, [0])[0] for rel in chosen_rels]) / 
                            len(chosen_rels)) / self.max_log_freq, 1.0)
                        conf = conf*0.9
                
                if retrieved_paths:
                    paths_batch.append(retrieved_paths[0])
                    conf_batch.append(conf)
                else:
                    paths_batch.append([])
                    conf_batch.append(0.0)
            else:
                paths_batch.append([])
                conf_batch.append(0.4)
        tm_end = time.time()
        log.info(f"Dialog path ranker time: {tm_end-tm_st}")
                
        return paths_batch, conf_batch
