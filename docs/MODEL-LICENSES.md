# Model and Provider Licenses

Arya's own code is licensed under [Apache-2.0](../LICENSE). The AI models and hosted services Arya connects to are **not** covered by that license: each carries its own license or terms of service, and you use them under your own accounts and API keys. Arya distributes no model weights; local models are downloaded onto your machine by the `ollama-init` / `speaches-init` services at first start, and cloud models are accessed with keys you provide.

This file is informational and is not legal advice. Before deploying an agent commercially, verify the current terms at the linked sources; they change.

## Summary

| Provider pipeline | Stage | Model | License / governing terms | Commercial use |
|---|---|---|---|---|
| OpenAI | STT | `gpt-4o-mini-transcribe` (and catalog alternatives) | [OpenAI Business Terms](https://openai.com/policies/may-2025-business-terms/) | Yes. You own the output; outputs may not be used to train competing models |
| OpenAI | LLM | `gpt-4o-mini` (and catalog alternatives) | [OpenAI Business Terms](https://openai.com/policies/may-2025-business-terms/) | Yes, same terms |
| OpenAI | TTS | `gpt-4o-mini-tts` (and catalog alternatives) | [OpenAI Business Terms](https://openai.com/policies/may-2025-business-terms/) | Yes, same terms |
| Groq | STT | Whisper `large-v3-turbo` | [MIT](https://github.com/openai/whisper) | Yes |
| Groq | LLM | GPT-OSS 120B | [Apache-2.0](https://huggingface.co/openai/gpt-oss-120b) | Yes |
| Groq | TTS | Orpheus (`canopylabs/orpheus-v1-english`) | [Apache-2.0 per Canopy Labs](https://huggingface.co/canopylabs/orpheus-3b-0.1-ft); Llama 3.2 derivative, see note 2. One-time terms acceptance in the [Groq console](https://console.groq.com/docs/model/canopylabs/orpheus-v1-english) | Yes, with note 2 |
| Open source (local) | STT | faster-whisper (Systran conversions) | [MIT](https://huggingface.co/Systran/faster-whisper-base) | Yes |
| Open source (local) | LLM | Qwen3.5 2B via Ollama (`qwen3.5:2b`) | [Apache-2.0](https://ollama.com/library/qwen3.5:2b) | Yes. Same license for the suggested `qwen3.5:4b` |
| Open source (local) | TTS | Kokoro-82M via Speaches | [Apache-2.0](https://huggingface.co/hexgrad/Kokoro-82M) | Yes |
| Open source (local) | Embeddings | `nomic-embed-text` (v1.5) | [Apache-2.0](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) | Yes |
| vLLM / custom Ollama | any | the model you configure | that model's own license | Your responsibility: check the license of every model id you point Arya at |

Hosted services used by optional abilities are governed by their own plans and terms: web search (SerpAPI, Traversaal Ares, Tavily) and memory (Mem0). Groq's free tier is a rate-limited tier of a commercial service governed by the [Groq Services Agreement](https://console.groq.com/docs/legal/services-agreement); your inputs and outputs remain yours and are not used for training.

## Notes

### 1. Llama models (only if you point Ollama at one yourself)

The local provider defaults to Qwen3.5 2B, and every model in Arya's Ollama suggestions list is Apache-2.0 or MIT, so a stock install carries no attribution, naming, or user-count conditions. Llama 3.2 3B (the previous default) is no longer pulled or suggested. If you type a Llama tag into the Ollama model field yourself, the Llama 3.2 Community License applies: it permits commercial use yet is not an OSI-approved open-source license, and if you build a product on it:

- Prominently display **"Built with Llama"** on related product pages or documentation, and include the license with any redistribution.
- Products exceeding **700 million monthly active users** require a separate license from Meta.
- Fine-tuned derivatives must carry **"Llama"** at the start of the model name.
- Use is subject to [Meta's Acceptable Use Policy](https://www.llama.com/llama3_2/use-policy/).
- The EU restriction in Llama 3.2 applies to the multimodal (vision) models only; the text-only 3B used here is not geographically restricted.

To avoid these conditions entirely, stay on the default Qwen3.5 tags or another suggested Apache or MIT licensed model (Granite, Phi-4-mini; see `agents.md`, "Swap the local models").

One Qwen trap: the Qwen2.5 family is Apache-2.0 at most sizes, but the **3B and 72B** sizes are under the non-commercial [Qwen Research License](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct). `qwen2.5:3b` is therefore not in Arya's suggestions; all Qwen3 and Qwen3.5 sizes are Apache-2.0.

The same class of terms applies to other Llama-family models you may serve via vLLM (for example Llama 3.1 8B under the Llama 3.1 Community License).

### 2. Orpheus (Groq TTS)

Canopy Labs distributes Orpheus under Apache-2.0, with model-card restrictions of their own: no impersonation without consent, no misinformation or deception (including fraudulent calls), no illegal or harmful use. Orpheus is fine-tuned from Llama 3.2 3B, and Meta's license obliges derivatives to remain under the Llama 3.2 Community License; the conservative reading is that the conditions in note 1 flow through to Orpheus. For a commercial deployment using the Groq pipeline's voice, we recommend treating Orpheus as Llama-licensed (display "Built with Llama") and honoring Canopy's restrictions, which a voice agent should honor regardless.

Groq requires accepting this model's terms once in their console before the API serves it; Arya surfaces the resulting error on first use if the terms have not been accepted.

### 3. Free tiers are not commercial licenses everywhere

Some voice providers license free-tier output for non-commercial use only. ElevenLabs (not currently integrated in Arya) is the prominent example: its [Terms of Service](https://elevenlabs.io/terms-of-use) restrict free users to non-commercial purposes, with a commercial license included from the first paid plan up. If you extend Arya with such a provider, check the tier you are on before deploying the agent publicly.

## Your responsibilities as a deployer

- Local testing on your own machine raises no practical licensing concerns with any shipped pipeline.
- A public, commercial deployment must satisfy both halves: the **model license** (the weights) and the **provider's terms** (the service your key belongs to).
- Arya grants no rights to any third-party model. Bring-your-own-key means the agreement is between you and the provider.
