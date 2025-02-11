import os
import sys

import fire
import json
from tqdm import tqdm
from num2words import num2words
import torch
import transformers
from peft import PeftModel
from transformers import GenerationConfig, LlamaForCausalLM, LlamaTokenizer

from utils.callbacks import Iteratorize, Stream
from utils.prompter import Prompter

if torch.cuda.is_available():
    device = "cuda"
else:
    device = "cpu"

try:
    if torch.backends.mps.is_available():
        device = "mps"
except:  # noqa: E722
    pass

def postprocess(text):
    text_orth = text
    text = text_orth.strip().lower()
    text = text.replace("-", " ")
    text = ''.join(c for c in text if c.isalnum() or c == "'" or c == " ")
    words = []
    for word in text.split():
        if word.isdigit():
            words.append(num2words(int(word)))
        else:
            words.append(word)
    text = ' '.join(words)
    return text_orth.strip().strip("\n").strip('\"'), text

# def postprocess(text, split_string="###"):
#     text_orth = text.split(split_string)[0].strip("\n")
#     text = text_orth.strip().lower()
#     text = text.replace("-", " ")
#     text = ''.join(c for c in text if c.isalnum() or c == "'" or c == " ")
#     words = []
#     for word in text.split():
#         if word.isdigit():
#             words.append(num2words(int(word)))
#         else:
#             words.append(word)
#     text = ' '.join(words)
#     return text_orth.strip().strip("\n").strip('\"'), text


def main(
    load_8bit: bool = False,
    base_model: str = "",
    lora_weights: str = "tloen/alpaca-lora-7b",
    prompt_template: str = "",  # The prompt template to use, will default to alpaca.
    server_name: str = "0.0.0.0",  # Allows to listen on all interfaces by providing '0.
    share_gradio: bool = False,
):
    base_model = base_model or os.environ.get("BASE_MODEL", "")
    assert (
        base_model
    ), "Please specify a --base_model, e.g. --base_model='decapoda-research/llama-7b-hf'"

    prompter = Prompter(prompt_template)
    tokenizer = LlamaTokenizer.from_pretrained(base_model)
    if device == "cuda":
        model = LlamaForCausalLM.from_pretrained(
            base_model,
            load_in_8bit=load_8bit,
            torch_dtype=torch.float16,
            device_map="auto",
        )
        model = PeftModel.from_pretrained(
            model,
            lora_weights,
            torch_dtype=torch.float16,
        )
    elif device == "mps":
        model = LlamaForCausalLM.from_pretrained(
            base_model,
            device_map={"": device},
            torch_dtype=torch.float16,
        )
        model = PeftModel.from_pretrained(
            model,
            lora_weights,
            device_map={"": device},
            torch_dtype=torch.float16,
        )
    else:
        model = LlamaForCausalLM.from_pretrained(
            base_model, device_map={"": device}, low_cpu_mem_usage=True
        )
        model = PeftModel.from_pretrained(
            model,
            lora_weights,
            device_map={"": device},
        )

    # unwind broken decapoda-research config
    model.config.pad_token_id = tokenizer.pad_token_id = 0  # unk
    model.config.bos_token_id = 1
    model.config.eos_token_id = 2

    if not load_8bit:
        model.half()  # seems to fix bugs for some users.

    model.eval()
    if torch.__version__ >= "2" and sys.platform != "win32":
        model = torch.compile(model)

    def evaluate(
        instruction,
        input=None,
        temperature=0.1,
        top_p=0.75,
        top_k=40,
        num_beams=4,
        max_new_tokens=128,
        stream_output=False,
        **kwargs,
    ):
        prompt = prompter.generate_prompt(instruction, input)
        inputs = tokenizer(prompt, return_tensors="pt")
        input_ids = inputs["input_ids"].to(device)
        generation_config = GenerationConfig(
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            num_beams=num_beams,
            **kwargs,
        )

        generate_params = {
            "input_ids": input_ids,
            "generation_config": generation_config,
            "return_dict_in_generate": True,
            "output_scores": True,
            "max_new_tokens": max_new_tokens,
        }

        # Without streaming
        with torch.no_grad():
            generation_output = model.generate(
                input_ids=input_ids,
                generation_config=generation_config,
                return_dict_in_generate=True,
                output_scores=True,
                max_new_tokens=max_new_tokens,
            )
        s = generation_output.sequences[0]
        output = tokenizer.decode(s)
        return prompter.get_response(output)

    # testing code for readme

    utts = {}
    for rank in range(16):
        with open(f'/datablob/v-yuangli/SPGI/decode_hubert/decode.{rank}.json') as f:
            utts_ = json.load(f)
        utts.update(utts_)

    # utts = {}
    # for rank in range(4):
    #     with open(f'/datablob/v-yuangli/SPGI/decode_whisper/decode.{rank}.json') as f:
    #         utts_ = json.load(f)
    #     utts.update(utts_)

    for _ in range(10):
        # for k in utts:
        for k in tqdm(utts):
            # print(utts[k]["nbest_hyp"][0].strip().lower())
            res = evaluate("Correct the spelling errors of the following sentence:", utts[k]["nbest_hyp"][0].strip().lower())
            # res = evaluate("Correct the spelling errors of the following sentence:", utts[k]["hyp"].strip())
            # print(res)
            result_orth, result = postprocess(res)
            # print(result)
            # print(result_orth)
            # print("***************")
            utts[k]["hyp"] = result
            utts[k]["hyp_spgi"] = result_orth
        if _ == 0:
            with open("/datablob/v-yuangli/SPGI/fix_spelling_7b_0shot_alpaca_spelling.json", "w") as output_file:
                json.dump(utts, output_file, indent=4)

if __name__ == "__main__":
    fire.Fire(main)
