# Metrics and evals reference

Every call Arya runs is measured automatically. Nothing needs to be switched on for the timing and behavior numbers; only the AI-judged quality checks are opt-in (Options, "Evaluate my agent's answers"). This page lists every number the product records, what it means, how it is measured, and where to read it.

Two ideas to keep in mind while reading:

- **Measurement is the same for every provider.** The timers sit inside the voice pipeline, so OpenAI, Groq, Gemini, Ollama, vLLM and Speaches are all measured identically. Only the readings differ, which is what makes comparisons meaningful.
- **A typed chat has no voice timings.** No speech in means nothing to wait for or transcribe; no speech out means no time to first audio. Those sessions are labeled "text session" and excluded from speed numbers.

## 1. Where the numbers live

| Place | What it holds |
|---|---|
| Insights screen | Aggregates per agent and date range: p50 and p95 per stage, behavior cards, quality scores, the per-call table |
| Transcript view | Per answer: the latency badge with its stage breakdown, tools that ran, judge marks, cut-off and spoken-format flags |
| `messages.metrics` (Postgres) | The per-turn report the SDK attaches to each message; drives the transcript and most dashboard cards |
| `turn_metrics` (Postgres) | One row per pipeline stage per turn plus one per tool call; survives rebuilds, grouped per stage and per model for benchmarks |
| `conversations.metadata` (Postgres) | Per-call usage summary, how the call ended, and the eval record |
| `rag_events` (Postgres) | One row per knowledge-base lookup: score, chunks kept, timing, errors |

## 2. Latency, per answer

Shown as p50 (typical) and p95 (worst case) on Insights; exact per answer on transcripts. All in seconds.

| Metric | Plain meaning | Measured from | Where shown |
|---|---|---|---|
| Waiting (end of turn) | How long the agent took to decide the caller had finished speaking | `end_of_turn_delay` on the caller's message | Transcript badge row; Insights stage bar |
| Hearing (transcription delay) | Time from the caller finishing to the transcript being ready | `transcription_delay` on the caller's message | Transcript badge row; Insights stage bar |
| STT duration | Raw processing time of the speech-to-text request. 0 when STT streams (OpenAI realtime), a real number for batch STT (Speaches, Groq, vLLM) | `STTMetrics.duration` in `turn_metrics` | Database only |
| Thinking (LLM time to first token) | Time from sending the request to the first word of the reply | `llm_node_ttft` on the agent's message | Transcript badge row; Insights stage bar |
| LLM full duration | Time to the complete reply text | `LLMMetrics.duration` in `turn_metrics` | Database only |
| Speaking (TTS time to first byte) | Time from sending the reply text to the first audio chunk | `tts_node_ttfb` on the agent's message | Transcript badge row; Insights stage bar |
| TTS full duration | Time to synthesize the whole reply | `TTSMetrics.duration` in `turn_metrics` | Database only |
| End to end | Caller finishes speaking to the agent starting to speak. When the SDK did not record it (common with streaming STT), the transcript shows the sum of the measured stages marked with ≈ and the dashboard counts the answer as "approximated" | `e2e_latency`, else `llm_node_ttft + tts_node_ttfb` | Transcript badge headline; Insights "Response speed" |
| Playback latency | Delay before the first audio frame actually plays | `playback_latency` on the agent's message | Database only |
| Tool time | Time spent inside each tool call (search, bookings, connectors). Sits inside end to end, so a slow tool looks like a slow agent unless you look here | `function_tools_executed` event, stored as `ToolCall` rows in `turn_metrics` and as `tools` on the agent's message | Transcript badge rows; Insights "Tool calls" card |
| Cold-start penalty | How much slower the first timed answer of a call is than the median of that call's later answers, averaged over calls. Local stacks pay model-load costs on turn one and a plain average hides it | Computed in the Insights endpoint from per-answer response times | Insights, under Response speed |

How to read the stage bar: the four segments are drawn in proportion to their p50, so the widest segment is where a typical answer loses its time. On a CPU-only local stack expect Speaking (Kokoro) to dominate; on a cloud stack expect Thinking.

## 3. Throughput and usage

| Metric | Plain meaning | Measured from | Where shown |
|---|---|---|---|
| Tokens in and out | Prompt and completion tokens per LLM turn, plus cached tokens | `LLMMetrics` in `turn_metrics`; per-model totals in the call's usage summary | Database only |
| TTS characters and audio seconds | Text synthesized and audio produced per turn | `TTSMetrics` in `turn_metrics` | Database only |
| Speaking rate | Characters of text per second of produced audio. A config knob: too high reads as rushed | `sum(chars) / sum(audio seconds)` over TTS rows | Insights, under Response speed |
| STT audio seconds | Caller audio transcribed per turn | `STTMetrics.audio_duration` | Database only |
| VAD and turn-detector time | Inference time of the local voice-activity and end-of-turn models | `VADMetrics`, `EOTInferenceMetrics` in `turn_metrics` | Database only |

## 4. Conversation behavior

| Metric | Plain meaning | Measured from | Where shown |
|---|---|---|---|
| Calls, average length, turns per call | Volume and shape of traffic in the window | `conversations` and `messages` | Insights cards |
| Interrupted | Share of voice calls (only those recorded after the flag was introduced; older calls are left out, not counted as zero) where the caller cut the agent off mid-reply, and the total count. Some is normal; a high share means replies run too long; near zero next to many "ended after silence" calls means callers never get a word in | `interrupted` flag the SDK sets on the agent's message | Insights "Interrupted" card; "cut off" mark under the reply; count in the transcript stats line |
| How calls end | Caller ended, ended after silence (3-minute inactivity watchdog), hit the time limit, connection lost, unknown | `ended_reason` stamped by the API, the worker's watchdog, and the LiveKit disconnect reason | Insights "How calls end"; transcript header |
| Turned away | Visitors refused by the simultaneous-calls cap, the daily cap, or the rate limiter | `session_refusals`, aggregated per minute | Insights "Turned away"; "Refused today" on Share and embed |
| Text sessions | Typed chats in the window, excluded from every speed number | No answer carries voice timings | Insights, under Response speed; transcript stats line |

## 5. Knowledge base

| Metric | Plain meaning | Measured from | Where shown |
|---|---|---|---|
| Document usage | Of the answers that received document context, how many drew on it. Read it as "is my knowledge base being used", not "is my agent good": context gets attached whenever retrieval scores above the floor, including on questions the documents do not really cover, and a correct answer from the prompt or web search then counts as "not used". The main number is the word-overlap check, which runs on every call; when evaluation is on, the AI judge's reading of the answers it saw with their excerpts is shown beside it, and that reading also catches paraphrase | Document citations on answers versus injected retrievals; the judge's per-answer `used_context` verdict | Insights "Document usage"; "Used your documents" verdict on transcripts (never scored) and a "documents used / not used" mark under each judged reply |
| Faithfulness | Of the judged answers that received document context, how many made no claim about the business (prices, hours, policies, products, procedures) that the excerpts contradict or do not contain. The RAG hallucination number: a correct-sounding fee that is not in the documents fails it | The AI judge, per answer, shown the excerpts the agent was given (section 6) | Insights "Faithfulness" card and the "Answers against the documents" chart; "faithful" mark under each judged reply |
| Context relevance | Of the same answers, how many got excerpts that covered the question. Grades the lookup and the documents, not the agent, which is why it is never counted in the quality score | The same judge call | Insights "Context relevance" card and chart; "context" mark under the reply; verdict on the transcript, never scored |
| Knowledge base match | Median similarity of document lookups to what callers asked, 0 to 1, plus lookup count, timeouts and median lookup time | `rag_events` | Insights "Knowledge base" |
| Lookup time | How long a lookup took, from the caller finishing to the excerpts being ready, median per bucket over time. Sits inside the response time | `rag_events` | Insights Knowledge base tab, "Lookup time (median)" |

## 6. Quality (AI-judged evals, opt-in)

Judged after the call ends, in the background, by a cloud model (OpenAI, Groq or Gemini; the pipeline under test can be fully local, only the grading goes to the cloud). Each check returns pass, fail or maybe.

| Check | Scope | How it counts | What it catches |
|---|---|---|---|
| Task completion | Whole call | One verdict | The caller's request was left unmet |
| Conciseness | Whole call | One verdict | Rambling, repetition |
| Consistency | Whole call | One verdict | The agent contradicting itself across turns |
| Tool usage | Whole call, only when the agent has tools | One verdict | Wrong tool, bad parameters, misread results |
| Accuracy | Per answer | Worst answer counts | A single invented or unsupported fact, which a whole-call average would hide |
| Relevancy | Per answer | Averaged | A pattern of ignoring the caller or drifting off topic |
| Faithfulness | Per answer that received document context | Worst answer counts | A claim about the business that the documents do not support: the RAG hallucination |
| Context relevance | Per answer that received document context | Averaged; shown, never scored | Lookups that bring back excerpts about something else |
| Used your documents | Whole call | Shown, never scored | See Document usage above. The judge's reading when per-answer judging covered the answers with context, the word-overlap count otherwise (the verdict's reasoning says which, and quotes the other) |
| Spoken format | Per answer, deterministic | Shown, never scored | Long raw digit strings, URLs, markdown: text that reads badly aloud |

**Score** = pass 1, maybe 0.5, fail 0, averaged over the scored checks that ran (accuracy and faithfulness contribute their worst answer, relevancy its average). No score is shown when any selected check failed to run, so a number is never computed off a partial run; the transcript says how many checks ran instead.

**Per-answer judging** uses one combined LLM call per substantive answer (greeting and "thanks, bye" turns are skipped) with the last few exchanges as context. An answer that received document context carries the excerpts the agent was given into that same call (up to 1,200 characters per excerpt, 6,000 in all), and the judge returns three more verdicts: faithfulness, context relevance and whether the answer drew on the excerpts. No extra call, no extra wait. Each judged reply shows its marks under the bubble, reasoning on hover: accuracy and relevancy on every judged reply, faithful, context and documents used on the replies that had excerpts.

**Backup judge.** Any check the primary judge model fails on (rate limit, model access, a flaky verdict) is retried on a second model of the same provider; the transcript notes which checks used it. Defaults: OpenAI `gpt-4.1-mini` then `gpt-4o-mini`; Groq `openai/gpt-oss-120b` then `openai/gpt-oss-20b`; Gemini `gemini-3.5-flash-lite` then `gemini-3.5-flash`. Overridable with `EVALS_JUDGE_MODEL_*` and `EVALS_JUDGE_FALLBACK_*` environment variables on the worker.

## 7. Not measured yet

- STT word error rate: needs the same recorded audio played into each configuration with a known transcript (golden test set).
- Turn-taking errors: the agent starting while the caller is still speaking, and dead-air gaps. Deferred until enough timing data exists to set the thresholds from real calls.
- Interruption latency is now measured, with a limit: see section 9.

## 8. Benchmarking a configuration

Nothing to run. Make the calls, then read Insights.

1. **One agent per configuration.** Duplicate the agent (same prompt, same documents) and change only the models. The Agent filter on Insights then isolates each configuration.
2. **Keep the judge constant.** Set "Judge runs on" to the same cloud provider for every configuration, so quality scores compare pipelines, not judges.
3. **Same spoken script, several times.** Eight to ten questions that hit the documents (fees, hours, an account number, a dress code) plus one off-topic question, spoken in the same order, five calls per configuration, back to back in the same hour. Include the first call after a restart so the cold-start number is real. Spoken, not typed.
4. **Keep context comparable.** Same documents, same prompt, similar call length. Small local models slow down with context size; a longer call is a different benchmark.
5. **If a stage runs remotely** (vLLM on a GPU box through a tunnel), remember the tunnel's round trip is inside that stage's number, and note the GPU type per run.
6. **Read:** the stage bar for where each configuration loses time; cold start for model-load cost; document usage and faithfulness for whether the documents are used and respected; accuracy worst-turn and spoken format for where a small model breaks.

The dashboard JSON for one agent:

```bash
curl -s "localhost:8000/api/insights/summary?agent_id=AGENT_ID&days=1" | python3 -m json.tool
```

Every configuration side by side, per stage and model, from the database:

```bash
docker exec arya-postgres-1 psql -U arya -d arya -c "SELECT c.metadata->>'agent_name' AS agent, t.metric_type, t.model, count(*) AS turns, round(percentile_cont(0.5) WITHIN GROUP (ORDER BY t.stage_ms)) AS p50_ms, round(percentile_cont(0.95) WITHIN GROUP (ORDER BY t.stage_ms)) AS p95_ms FROM turn_metrics t JOIN conversations c ON c.room_name = t.room_name WHERE t.created_at > now() - interval '1 day' AND t.stage_ms IS NOT NULL GROUP BY 1,2,3 ORDER BY 1,2,3;"
```

Column meanings: `EOUMetrics` is Waiting, `STTMetrics` is STT duration, `LLMMetrics` is Thinking, `TTSMetrics` is Speaking, `ToolCall` is tool time; `model` is the configured model name and, for local stacks, `provider` in the table is the host it ran on.

## 9. Trends, breakdowns and the call timeline

Everything above is also drawn over time and per call. Nothing here is a new measurement except the three marked **new collection**; the rest reads the same stored data.

### 9.1 The dashboard tabs (Insights screen)

The dashboard is split into tabs, each a few cards and charts. Overview is what opens first: four numbers and three charts. The tab rides in the URL (`#/insights?tab=kb`), so a link opens on the right one. Quality, Knowledge base and Tools appear only when the window has data for them. Every chart follows the date range and the Granularity control (hourly up to two days, daily beyond, or set it by hand to hourly, daily or weekly, Monday to Sunday). Lines carry no standing dots; hovering (or the arrow keys) shows the bucket's values in a tooltip with a dot per series, highest value first. A bucket with no calls is drawn at the baseline so the line stays continuous, and on hover its dot is hollow and the tooltip says "no data" (or one line, "No data for this day", when no series has a reading), so a real 0% and a day nothing was measured never look the same. Every chart has a chart-type switch: line, bars (stacked when the series are parts of a whole such as endings or errors, side by side when they are independent measures such as a pass rate per check, where a missing bucket is simply no bar), totals (one ranked bar per series over the range, counts only) and table. Counts and durations also take a Linear | Log axis switch in the line and side-by-side views: the log axis keeps a quiet week readable next to one spike, with decade ticks, zero and "no data" on the baseline. Percentages and stacks stay linear (a stack stops adding up on a log axis). Count charts carry a headline total for the range. The call list is its own tab, Conversations, last in the strip, with one line above it saying how many calls it holds. Click a bucket with data on any chart to open that tab narrowed to the calls in the bucket (and to one kind of call where the chart is about one, such as cut-off replies or scored calls); the chip above the list clears the filter, and the filter stays while you move between tabs until the range or source changes. The expand control at the right of a panel title opens the same chart over the page, wider and taller, with the same switch and tooltip. Each chart has a CSV download.

| Tab | Cards | Charts |
|---|---|---|
| Overview | Calls, Avg call, Response speed, Errors | Calls (with text sessions and non-engaged calls), response time (typical p50, p90, p95), error rate |
| Calls | How calls end, Interrupted, Turned away | Talk time, simultaneous calls against the cap, how calls end, calls with a cut-off reply, refused calls |
| Speed | Response speed, First answer (cold start), Speaking rate | Response time, where the time goes per stage, how fast the agent stops when interrupted (new collection), by model (typical and worst time per stage, per model and provider: the benchmark table from section 8, without the query) |
| Quality | none | Score per bucket (complete runs only; a hollow point on hover means fewer than 3 scored calls), pass rate per check, weakest checks first |
| Knowledge base | Document usage, Faithfulness, Context relevance, Knowledge base match | Lookups, errors and timeouts; match quality; lookup time; answers against the documents (faithfulness and context relevance pass rate, when evaluation is on); document references over time (the five most retrieved documents, the rest as Other); documents cited and never cited |
| Tools | Tool calls (median time per tool) | Calls and failures, slowest tools |
| Errors | Errors | Error rate, errors by kind, pipeline errors by stage (new collection) |
| Usage | none | LLM tokens and spoken audio minutes. No prices, by decision |

**Response speed card** shows the typical (p50) and worst-case (p95) time to first word and the answer count. The stage split (Waiting, Hearing, Thinking, Speaking) is on the Speed tab, not on the card.

**By model** names the backend that served each stage, not who trained the model (that is in the model name): Gemini, OpenAI or Groq for the cloud plugins, LiveKit for its own end-of-turn model (downloaded once and run on CPU inside the worker, which is why its worst case can be a cold load on the first turn), and for the OpenAI-compatible plugins, which only record the host they talked to, the host is resolved to Speaches, Ollama or vLLM from the deployment's URLs, with "(local)" for this machine and the host otherwise. An unrecognised host is shown as stored.

**Document references** counts every lookup that injected a document, whether or not the answer then used it, so it reads against the Documents cited list: a document retrieved often and cited rarely is being pulled in for questions it does not answer.

**Error rate** is one number for "something is wrong": failed tool calls, knowledge-base errors and timeouts, failed evaluation runs, and calls that ended as connection lost, divided by answers plus lookups plus tool calls in the same bucket. A bucket with nothing in it has no rate (a gap), not 0%.

**Non-engaged calls** are calls with fewer than two caller turns (a greeting and a hangup). They are counted on the Calls card but are not yet excluded from the averages.

**Calls in progress** are not on the dashboard. The live count against the cap is on the Publish screen next to the deployment it belongs to, and the Calls tab's Simultaneous calls chart shows it over time. The series endpoint still returns `open_now` (same 15-minute window as the quota gate) for anyone reading the API.

### 9.2 The call timeline

Open any call and choose Timeline above the transcript. Each turn is drawn as bars on one time axis: Waiting, Thinking, Streaming (the rest of the reply), Voice (time to first audio) and the audio itself on the path the caller feels, with Hearing and tool calls on a second lane because they run alongside. A reply the caller cut off has a red end.

It is rebuilt from the stored stage metrics: each SDK metric is an end time plus a duration. Thinking can start before Waiting ends, because the model begins on the interim transcript while the turn is still being confirmed. Calls from before stage metrics were stored, and typed sessions, show an explanation instead of an empty chart.

### 9.3 New collection and its limits

| What | How | Limit |
|---|---|---|
| Pipeline errors | The session's error event, one row per STT, LLM or TTS failure, marked recovered or fatal | Only errors the SDK raises after its own retries. A retry that succeeds silently is not counted |
| Interruption latency | The caller starts speaking while the agent speaks; the agent stops within 3 seconds; the caller is still speaking or stopped within 0.6 seconds | Derived from state changes, not reported by the SDK. A short "mm-hm" that ends well before the reply does is ignored; one that ends just as the reply finishes on its own can still be counted |
| Files per lookup | `rag_events.files` records which documents a lookup injected | Older rows have none, so the retrieved count is only known from the day it shipped. The cited count covers all history |

Verify the three on a real call: talk over the agent once and an Interruption row appears; make the voice service fail mid-call and a fatal error row appears; ask a question that hits a document and `rag_events.files` holds its name.
