import logging
from typing import Optional

import torch
from torch import nn
from transformers import AutoModelForSeq2SeqLM, AutoModelForCausalLM, MODEL_FOR_CAUSAL_LM_MAPPING, \
    MODEL_FOR_SEQ_TO_SEQ_CAUSAL_LM_MAPPING

logger = logging.getLogger(__name__)

class ModelBase(nn.Module):
    def forward(self, batch):
        raise NotImplementedError

    @staticmethod
    def from_config(config, **kwargs) -> "ModelBase":
        task_mapping = [
            (MODEL_FOR_CAUSAL_LM_MAPPING, DecoderModel),
            (MODEL_FOR_SEQ_TO_SEQ_CAUSAL_LM_MAPPING, EncoderDecoderModel),
        ]
        config_name = config.__class__
        for transformer_model_mapping, model in task_mapping:
            transformer_model_name = transformer_model_mapping.get(config_name, None)
            if transformer_model_name is not None:
                return model(config=config, **kwargs)

        raise NotImplementedError


def get_gpus_max_memory(max_memory):
    max_memory = {i: max_memory for i in range(torch.cuda.device_count())}
    return max_memory


class EncoderDecoderModel(ModelBase):
    def __init__(self, config, model_name_or_path: Optional[str], **kwargs):
        """

        Args:
            config:
            model_name_or_path:
            parallelize:
            device: if parallelize = False, then we use specified device.
        """
        super(EncoderDecoderModel, self).__init__()
        logger.info("Building EncoderDecoderModel")
        if model_name_or_path:
            self._model = AutoModelForSeq2SeqLM.from_pretrained(
                model_name_or_path,
                cache_dir="/users/zyong2/data/zyong2/huggingface",
                from_tf=bool(".ckpt" in model_name_or_path),
                config=config,
                torch_dtype=kwargs.get("torch_dtype", None),
                # device_map="auto",
                # offload_folder="offload",
                # max_memory=get_gpus_max_memory("50GB"),

            )
        else:
            logger.info("Training new model from scratch")
            self._model = AutoModelForSeq2SeqLM.from_config(
                config,
                torch_dtype=kwargs.get("torch_dtype", None),
            )


    def forward(self, batch, **kwargs) -> torch.Tensor:
        model_inputs = {
            k: batch[k]
            for k in ["input_ids", "attention_mask", "labels"]
        }
        print("Got device", model_inputs["input_ids"].device, self._model.device)
        logits = self._model(**model_inputs).logits.to(torch.float32)
        masked_log_probs = batch["labels_attention_mask"].unsqueeze(-1) * torch.log_softmax(logits, dim=-1)
        seq_token_log_probs = torch.gather(masked_log_probs, -1, batch["labels"].unsqueeze(-1))
        seq_log_prob = seq_token_log_probs.squeeze(dim=-1).sum(dim=-1)
        seq_log_prob = seq_log_prob.view(batch["targets"].size(0),
                                         -1)  # TODO(Victor): this reshapes works based on the assumption that all examples have the same number of choices. the pre-processing doesn't make this assumption.
        predictions = seq_log_prob.argmax(dim=-1)
        return predictions

def _expand_mask(mask: torch.Tensor, dtype: torch.dtype, tgt_len: int = None):
    """
    Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]`.
    """
    batch_size, source_length = mask.size()
    tgt_len = tgt_len if tgt_len is not None else source_length

    expanded_mask = mask[:, None, None, :].expand(batch_size, 1, tgt_len, source_length).to(dtype)

    inverted_mask = 1.0 - expanded_mask

    return inverted_mask.masked_fill(inverted_mask.to(torch.bool), -torch.inf)

class DecoderModel(ModelBase):
    def __init__(self, config, model_name_or_path: Optional[str], **kwargs):
        super(DecoderModel, self).__init__()
        logger.info("Building DecoderModel")
        if model_name_or_path:
            self._model = AutoModelForCausalLM.from_pretrained(
                model_name_or_path,
                cache_dir="/users/zyong2/data/zyong2/huggingface",
                config=config,
                torch_dtype=kwargs.get("torch_dtype", None),
                use_auth_token=kwargs.get("use_auth_token", None)
                # # Necessary for pipeline parallelism:  # commenting out because adapter-transformers don't support
                # device_map="auto",
                # max_memory=get_gpus_max_memory("50GB"),
                # offload_folder="offload",
            )
        else:
            logger.info("Training new model from scratch")
            self._model = AutoModelForCausalLM.from_config(
                config,
                torch_dtype=kwargs.get("torch_dtype", None),
                use_auth_token=kwargs.get("use_auth_token", None),
            )
        
        if kwargs.get("adapter_dir", None):
            print(f"load language adapters ({kwargs.get('adapter_dir', None).split('/')[-1]})")
            lang_adapter_name = self._model.load_adapter(kwargs.get("adapter_dir", None))
            self._model.set_active_adapters(lang_adapter_name)

    def forward(self, batch, prefixlm=False):
        device = batch["input_ids"].device
        bs, prefix_len = batch["input_ids"].shape

        model_inputs = {
            # Shape [bs * answer_choices, seq_len]
            "input_ids": torch.cat([batch["input_ids"], batch["labels"]], dim=-1),
            "attention_mask": torch.cat([batch["attention_mask"], batch["labels_attention_mask"]], dim=-1),
        }
        # Set position ids correctly to take care of padding tokens between inputs_ids and labels
        position_ids = torch.maximum(
            torch.cumsum(model_inputs["attention_mask"].to(torch.long), dim=-1) - 1,
            torch.zeros(1, dtype=torch.long, device=device)[None, None]
        )
        model_inputs["position_ids"] = position_ids
        if prefixlm:
            bs, lab_len = batch["labels"].shape
            device = model_inputs["attention_mask"].device
            dtype = torch.float32

            # Get mask for input & target padding
            mask = _expand_mask(model_inputs["attention_mask"], dtype)

            # Create causal mask for targets
            labels_causal_mask = (1 - torch.tril(torch.ones((bs, 1, lab_len, lab_len), device=device, dtype=dtype)))
            labels_causal_mask = torch.cat([torch.ones((bs,1,prefix_len,lab_len), device=device, dtype=dtype),labels_causal_mask], dim=-2)

            # Add causal mask for targets & let inputs attend bidirectionally
            mask[:, :, :, prefix_len:] += labels_causal_mask.masked_fill(labels_causal_mask.to(torch.bool), -torch.inf)
            model_inputs["causal_mask"] = mask
        # Shape [bs * answer_choices, target_len, vocab]
        logits = self._model(**model_inputs).logits[:, prefix_len-1:-1].to(torch.float32)
        # Shape [bs * answer_choices, target_len, vocab]
        masked_log_probs = batch["labels_attention_mask"].unsqueeze(-1) * torch.log_softmax(logits, dim=-1)
        # Gather all answer choices -> Shape [bs * answer_choices, target_len, 1]
        seq_token_log_probs = torch.gather(masked_log_probs, -1, batch["labels"].unsqueeze(-1))
        # Get logprobs sum for each answer choice -> Shape [bs * answer_choices]
        seq_log_prob = seq_token_log_probs.squeeze(dim=-1).sum(dim=-1)
        # Shape [bs, answer_choices]
        seq_log_prob = seq_log_prob.view(batch["targets"].size(0),
                                         -1)  # TODO(Victor): this reshapes works based on the assumption that all examples have the same number of choices. the pre-processing doesn't make this assumption.
        # Get final answer prediction -> Shape [bs]
        predictions = seq_log_prob.argmax(dim=-1)
        return predictions
