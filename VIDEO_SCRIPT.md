# 3-Minute Video Script — GridWise

**Speaking time:** 378 words, which is **2:42 to 3:09** depending on your pace.
Lines marked **[cut if long]** can be dropped while recording; without them it is
355 words, or **2:32 to 2:58**.

The limit is hard at 3:00, so if you speak slowly, **drop the [cut if long]
lines and you are safe**. Time yourself once before the real take.

**How to use this:** the left column is what to show. The right column is what
to say. Short sentences are on purpose. Do not join them together. Pause at
every full stop.

**Before you record:**
1. Run `python run.py` and wait for "GridWise is ready".
2. Open `http://localhost:8000/ui/`.
3. Open a terminal beside it.
4. **Close your `.env` file** so no API key appears on screen.

---

## Part 1 — The Problem (0:00 – 0:25)

| Show on screen | Say this |
| :--- | :--- |
| Title: **GridWise — Smart Campus Energy Optimization** | Hello. This is GridWise, our solution for the energy challenge. |
| Picture: campus, solar panel, battery, grid | A campus uses grid power, solar, and a battery. The price changes every hour. |
| Show this note big on screen:<br>*"Solar output will drop to about 20% from 1 PM to 3 PM."* | Operators write short notes in plain English. |
| Highlight "20%" and "1 PM to 3 PM" | A computer cannot use this sentence. So we do two things. Understand the note. Then build the cheapest valid plan. |

---

## Part 2 — The Architecture (0:25 – 1:20)

**This is the most important part. Do not cut anything here.**

| Show on screen | Say this |
| :--- | :--- |
| Diagram: four boxes in a row | Our service has four steps. |
| Highlight box 1: **LLM** | One. A language model reads each note and turns it into a structured directive. Here, hours thirteen and fourteen, and a factor of zero point two. |
| Highlight box 2: **Guardrails** | Two. Guardrails. We never trust the model. We check every hour, every number, and every range. We check there is one answer per note. Anything wrong becomes no-op. We never invent a rule. |
| Highlight box 3: **Optimizer**, then show the LP formula | Three. The optimizer. We compared five solvers and chose a linear program, with SciPy and HiGHS. This problem has no battery loss. So we do not need integer variables. Our answer is not a guess. It is the true cheapest plan. |
| Show `optimization : 1.0000 average quality ratio` | On all ten public cases, our cost matches the organizer's best cost exactly. In about three milliseconds. |
| Highlight box 4: **Final replay** | Four. We replay our own plan and check every rule before we answer. |

---

## Part 3 — The Demo (1:20 – 2:10)

| Show on screen | Say this |
| :--- | :--- |
| The console at `localhost:8000/ui/` | Here is the service running. |
| Click **Upload JSON**, pick a file | Testers can upload their own JSON file, or paste it here. |
| Point at the green validation box | We check it in the browser first. If something is wrong, we show it and do not send it. |
| Click **Validate & Optimize** | Now we optimize. |
| Point at the first note card | Each note becomes one directive. This one is a battery reserve. |
| Point at the hour list, then the blue hour strip | You can see the hours it affects. In the list, and in the hour strip. |
| Point at the `no_op` card | This note is about a room booking. Not energy. So it is no-op. |
| Scroll to the 24-hour table | Below is the full plan. |
| Point at night rows, then evening rows | We charge at night, when power is cheap. Solar at midday. Battery in the evening. |
| Point at the colour bars | **[cut if long]** Orange is grid. Yellow is solar. Blue is the battery. |
| Point at the last row | And the battery ends exactly where it started. |

---

## Part 4 — Reliability and Testing (2:10 – 2:40)

| Show on screen | Say this |
| :--- | :--- |
| Show the model table in the README | Free API keys hit rate limits. This happened to us many times. |
| Point at the two provider rows | So we use two providers, Gemini and Groq, with separate limits. We race three models and take the first good answer. |
| Terminal: run `python tests/test_api.py` | Here is our test command. **[cut if long]** Ten public cases, and fifteen of our own. |
| Point at the summary lines | Zero failures. Thirty-seven out of thirty-seven directives correct. Quality ratio one point zero. |
| Show `run.py` in the file list | To run everything, one command. Python run dot p y. |
| Title slide with your live URL | Everything else is in our README. Thank you. |

---

## Hard Words — Practise These Once

| Word | How to say it |
| :--- | :--- |
| directive | di-REK-tiv |
| optimizer | OP-ti-my-zer |
| linear program | LIN-ee-ar PRO-gram |
| guardrail | GARD-rail |
| integer | IN-te-jer |
| milliseconds | MIL-i-sec-onds |
| SciPy | "sy-py" |
| Groq | "grock" |

---

## Checklist Before You Upload

- [ ] Video is **3 minutes or less**
- [ ] The problem is explained
- [ ] The four steps are shown: **LLM → guardrails → optimizer → replay**
- [ ] You said **why** you chose a linear program
- [ ] A live request is shown working
- [ ] The **hours** for each note are visible on screen
- [ ] The run command and the test command are shown
- [ ] No API key ever appears on screen

---

## If You Are Still Over 3 Minutes

Cut in this order. These matter least for scoring:

1. Part 1 — the campus picture line. Start from the operator note.
2. Part 3 — the last-row battery line.
3. Part 3 — "Testers can upload their own JSON file, or paste it here."

**Never cut Part 2**, or the reason for choosing a linear program. The rubric
uses this video only to break a tie, and it compares problem understanding,
architecture clarity, and the LLM-to-guardrail-to-optimizer flow.
