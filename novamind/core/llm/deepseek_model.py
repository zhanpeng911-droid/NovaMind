"""Preserve DeepSeek's provider-specific reasoning on tool-loop round trips."""
from langchain_openai import ChatOpenAI


class DeepSeekChatModel(ChatOpenAI):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        originals = self._convert_input(input_).to_messages()
        for message, source in zip(payload.get("messages", []), originals, strict=False):
            if message.get("role") == "assistant":
                message["reasoning_content"] = source.additional_kwargs.get("reasoning_content", "")
        return payload

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, raw.get("choices", []), strict=False):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if isinstance(reasoning, str):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        result = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        choices = chunk.get("choices") or []
        reasoning = (choices[0].get("delta") or {}).get("reasoning_content") if choices else None
        if result is not None and isinstance(reasoning, str):
            result.message.additional_kwargs["reasoning_content"] = reasoning
        return result
