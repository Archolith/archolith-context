"""Extraction quality audit — call gpt-4.1-mini directly, capture raw output, score accuracy/noise/atomicity.

This bypasses the proxy and goes straight to the extraction model so we can:
1. See raw model JSON output
2. Score facts against a known ground-truth
3. Measure JSON parse rate
4. Quantify noise (irrelevant/redundant facts)
5. Check atomicity (is each fact self-contained and atomic?)

Usage:
    python scripts/audit_extraction_quality.py
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx

from archolith_proxy.config import get_settings
from archolith_proxy.extractor.client import extract_facts, _parse_extraction_response
from archolith_proxy.extractor.prompts import SYSTEM_PROMPT, build_extraction_prompt
from archolith_proxy.models.dtos import ExtractionResult

# --- Ground-truth for 5-turn coding session ---
# Each turn has: user_message, simulated_assistant_response, expected_facts (ground truth)
# We compare extracted facts against ground truth to compute accuracy.

TURNS = [
    {
        "user": "Create a Python class called Calculator with an add method.",
        "assistant": (
            "I'll create a Calculator class with an add method.\n\n"
            "```python\nclass Calculator:\n    def add(self, a: float, b: float) -> float:\n        return a + b\n```\n\n"
            "The Calculator class has been created with an `add` method that takes two numbers and returns their sum."
        ),
        "ground_truth": [
            "Calculator class created",
            "Calculator has an add method",
            "add method takes two float parameters",
            "add method returns sum of inputs",
        ],
    },
    {
        "user": "Now add a subtract method to the Calculator class.",
        "assistant": (
            "I'll add a subtract method to the Calculator class.\n\n"
            "```python\nclass Calculator:\n    def add(self, a: float, b: float) -> float:\n        return a + b\n\n"
            "    def subtract(self, a: float, b: float) -> float:\n        return a - b\n```\n\n"
            "The subtract method has been added. It follows the same pattern as add."
        ),
        "ground_truth": [
            "subtract method added to Calculator",
            "subtract method takes two float parameters",
            "subtract method returns difference",
        ],
    },
    {
        "user": "Add error handling for non-numeric inputs to all methods.",
        "assistant": (
            "I'll add type checking to both methods.\n\n"
            "```python\nclass Calculator:\n    def add(self, a: float, b: float) -> float:\n        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n            raise TypeError('Inputs must be numeric')\n        return a + b\n\n"
            "    def subtract(self, a: float, b: float) -> float:\n        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n            raise TypeError('Inputs must be numeric')\n        return a - b\n```\n\n"
            "Both methods now raise TypeError for non-numeric inputs."
        ),
        "ground_truth": [
            "error handling added to all Calculator methods",
            "TypeError raised for non-numeric inputs",
            "isinstance check used for type validation",
        ],
    },
    {
        "user": "Write a unit test for the Calculator class using pytest.",
        "assistant": (
            "I'll create a test file.\n\n"
            "```python\n# test_calculator.py\nimport pytest\nfrom calculator import Calculator\n\n"
            "def test_add():\n    calc = Calculator()\n    assert calc.add(2, 3) == 5\n\n"
            "def test_subtract():\n    calc = Calculator()\n    assert calc.subtract(5, 3) == 2\n\n"
            "def test_add_type_error():\n    calc = Calculator()\n    with pytest.raises(TypeError):\n        calc.add('a', 1)\n\n"
            "def test_subtract_type_error():\n    calc = Calculator()\n    with pytest.raises(TypeError):\n        calc.subtract(2, 'b')\n```\n\n"
            "The test file covers normal operations and error handling."
        ),
        "ground_truth": [
            "test_calculator.py created with pytest tests",
            "tests cover add and subtract methods",
            "tests cover TypeError for non-numeric inputs",
        ],
    },
    {
        "user": "Add a multiply method and update the tests.",
        "assistant": (
            "I'll add multiply and update tests.\n\n"
            "```python\nclass Calculator:\n    def add(self, a: float, b: float) -> float:\n        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n            raise TypeError('Inputs must be numeric')\n        return a + b\n\n"
            "    def subtract(self, a: float, b: float) -> float:\n        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n            raise TypeError('Inputs must be numeric')\n        return a - b\n\n"
            "    def multiply(self, a: float, b: float) -> float:\n        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n            raise TypeError('Inputs must be numeric')\n        return a * b\n```\n\n"
            "Updated test_calculator.py with multiply tests."
        ),
        "ground_truth": [
            "multiply method added to Calculator",
            "multiply method has same error handling pattern",
            "test_calculator.py updated with multiply tests",
        ],
    },
]


async def call_extraction_raw(http_client: httpx.AsyncClient, turn_number: int, turn: dict) -> dict:
    """Call the extraction model and return raw response + parsed result."""
    settings = get_settings()

    user_prompt = build_extraction_prompt(
        turn_number=turn_number,
        user_message=turn["user"],
        assistant_response=turn["assistant"],
    )

    payload = {
        "model": settings.extractor_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
    }

    resp = await http_client.post(
        f"{settings.extractor_base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.extractor_api_key}",
            "Content-Type": "application/json",
        },
        content=json.dumps(payload).encode(),
    )
    resp.raise_for_status()
    data = resp.json()

    raw_content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})

    # Try parsing
    parsed = _parse_extraction_response(raw_content, turn_number)

    return {
        "turn": turn_number,
        "raw_content": raw_content,
        "parsed": parsed,
        "usage": usage,
        "ground_truth": turn["ground_truth"],
    }


def score_extraction(result: dict) -> dict:
    """Score an extraction result against ground truth."""
    parsed: ExtractionResult = result["parsed"]
    ground_truth: list[str] = result["ground_truth"]
    raw: str = result["raw_content"]

    # 1. JSON parse success
    json_parsed = True
    try:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)
        json.loads(text)
    except json.JSONDecodeError:
        json_parsed = False

    # 2. Fact accuracy: for each ground-truth fact, check if any extracted fact covers it
    facts_content = [f.get("content", "").lower() if isinstance(f, dict) else str(f).lower() for f in parsed.facts]
    matched_gt = []
    unmatched_gt = []
    for gt in ground_truth:
        gt_lower = gt.lower()
        # Check if any extracted fact contains key words from the ground truth
        gt_words = set(gt_lower.split()) - {"to", "the", "a", "an", "is", "has", "for", "with", "and", "in", "of"}
        best_match = 0
        for fc in facts_content:
            fc_words = set(fc.split())
            overlap = len(gt_words & fc_words) / len(gt_words) if gt_words else 0
            best_match = max(best_match, overlap)
        if best_match >= 0.5:
            matched_gt.append((gt, best_match))
        else:
            unmatched_gt.append((gt, best_match))

    accuracy = len(matched_gt) / len(ground_truth) if ground_truth else 0

    # 3. Noise ratio: facts that don't match any ground truth
    noise_facts = []
    signal_facts = []
    for i, f in enumerate(parsed.facts):
        fc = f.get("content", "").lower() if isinstance(f, dict) else str(f).lower()
        fc_words = set(fc.split()) - {"to", "the", "a", "an", "is", "has", "for", "with", "and", "in", "of"}
        best_match = 0
        for gt in ground_truth:
            gt_words = set(gt.lower().split()) - {"to", "the", "a", "an", "is", "has", "for", "with", "and", "in", "of"}
            overlap = len(fc_words & gt_words) / len(gt_words) if gt_words else 0
            best_match = max(best_match, overlap)
        if best_match >= 0.4:
            signal_facts.append((i, fc, best_match))
        else:
            noise_facts.append((i, fc, best_match))

    noise_ratio = len(noise_facts) / len(parsed.facts) if parsed.facts else 0

    # 4. Atomicity check: facts that are too long (>100 chars) or compound
    non_atomic = []
    for i, f in enumerate(parsed.facts):
        fc = f.get("content", "") if isinstance(f, dict) else str(f)
        if len(fc) > 150:
            non_atomic.append((i, fc, len(fc)))
        # Check for compound sentences (multiple clauses)
        if " and " in fc.lower() and len(fc) > 80:
            non_atomic.append((i, fc, "compound"))

    # 5. Fact type distribution
    type_dist = {}
    for f in parsed.facts:
        ft = f.get("fact_type", "unknown") if isinstance(f, dict) else "unknown"
        type_dist[ft] = type_dist.get(ft, 0) + 1

    # 6. Confidence distribution
    confidences = [f.get("confidence", 0) for f in parsed.facts if isinstance(f, dict)]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0

    return {
        "json_parsed": json_parsed,
        "total_facts_extracted": len(parsed.facts),
        "ground_truth_count": len(ground_truth),
        "matched_gt": len(matched_gt),
        "unmatched_gt": unmatched_gt,
        "accuracy": accuracy,
        "signal_facts": len(signal_facts),
        "noise_facts": len(noise_facts),
        "noise_facts_detail": [(i, fc[:80]) for i, fc, _ in noise_facts],
        "noise_ratio": noise_ratio,
        "non_atomic": len(non_atomic),
        "non_atomic_detail": [(i, fc[:80], reason) for i, fc, reason in non_atomic],
        "type_distribution": type_dist,
        "avg_confidence": round(avg_confidence, 2),
        "decisions": len(parsed.decisions),
        "files_touched": len(parsed.files_touched),
        "invalidated": len(parsed.invalidated_fact_ids),
        "usage": result.get("usage", {}),
    }


async def run_audit():
    """Run the full extraction quality audit."""
    settings = get_settings()

    print("=" * 70)
    print("EXTRACTION QUALITY AUDIT")
    print(f"Model: {settings.extractor_model}")
    print(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    all_results = []
    all_scores = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        for i, turn in enumerate(TURNS):
            turn_num = i + 1
            print(f"\n--- Turn {turn_num} ---")
            print(f"User: {turn['user'][:80]}")

            try:
                result = await call_extraction_raw(client, turn_num, turn)
                score = score_extraction(result)

                all_results.append(result)
                all_scores.append(score)

                # Print raw extraction output
                print(f"\nRaw model output:")
                print(result["raw_content"][:500])
                print(f"\nParsed facts: {score['total_facts_extracted']}")
                for f in result["parsed"].facts:
                    if isinstance(f, dict):
                        print(f"  - [{f.get('fact_type', '?')}] {f.get('content', '')[:80]} (conf={f.get('confidence', '?')})")
                    else:
                        print(f"  - [string] {str(f)[:80]}")

                print(f"\nScore:")
                print(f"  JSON parsed: {score['json_parsed']}")
                print(f"  Accuracy: {score['accuracy']:.1%} ({score['matched_gt']}/{score['ground_truth_count']} ground truth matched)")
                print(f"  Noise ratio: {score['noise_ratio']:.1%} ({score['noise_facts']}/{score['total_facts_extracted']} noise facts)")
                if score['noise_facts_detail']:
                    print(f"  Noise facts: {score['noise_facts_detail'][:3]}")
                print(f"  Non-atomic: {score['non_atomic']}")
                if score['non_atomic_detail']:
                    print(f"  Non-atomic: {score['non_atomic_detail'][:3]}")
                print(f"  Types: {score['type_distribution']}")
                print(f"  Avg confidence: {score['avg_confidence']}")
                print(f"  Decisions: {score['decisions']}, Files: {score['files_touched']}, Invalidated: {score['invalidated']}")

                if score['unmatched_gt']:
                    print(f"  MISSED ground truth:")
                    for gt, sim in score['unmatched_gt']:
                        print(f"    - '{gt}' (best similarity: {sim:.2f})")

                usage = score.get("usage", {})
                print(f"  Tokens: prompt={usage.get('prompt_tokens', '?')}, completion={usage.get('completion_tokens', '?')}")

                # Rate limit courtesy
                await asyncio.sleep(2)

            except Exception as e:
                import traceback
                print(f"  ERROR: {e}")
                traceback.print_exc()
                all_scores.append({
                    "json_parsed": False,
                    "error": str(e),
                    "accuracy": 0,
                    "noise_ratio": 1.0,
                    "total_facts_extracted": 0,
                    "ground_truth_count": len(turn["ground_truth"]),
                    "matched_gt": 0,
                    "noise_facts": 0,
                    "non_atomic": 0,
                    "type_distribution": {},
                    "avg_confidence": 0,
                    "decisions": 0,
                    "files_touched": 0,
                    "invalidated": 0,
                    "unmatched_gt": [(gt, 0) for gt in turn["ground_truth"]],
                    "noise_facts_detail": [],
                    "non_atomic_detail": [],
                    "usage": {},
                })
                all_results.append({"raw_content": "", "parsed": ExtractionResult(facts=[], files_touched=[], decisions=[], invalidated_fact_ids=[], turn_number=turn_num), "ground_truth": turn["ground_truth"]})

    # --- Summary ---
    print("\n" + "=" * 70)
    print("AUDIT SUMMARY")
    print("=" * 70)

    valid_scores = [s for s in all_scores if "error" not in s]
    if not valid_scores:
        print("No valid extraction results — all turns failed.")
        return

    total_facts = sum(s["total_facts_extracted"] for s in valid_scores)
    total_gt = sum(s["ground_truth_count"] for s in valid_scores)
    total_matched = sum(s["matched_gt"] for s in valid_scores)
    total_noise = sum(s["noise_facts"] for s in valid_scores)
    total_non_atomic = sum(s["non_atomic"] for s in valid_scores)
    json_parse_rate = sum(1 for s in valid_scores if s["json_parsed"]) / len(valid_scores)

    overall_accuracy = total_matched / total_gt if total_gt else 0
    overall_noise = total_noise / total_facts if total_facts else 0
    overall_atomicity = 1 - (total_non_atomic / total_facts) if total_facts else 1

    total_prompt_tokens = sum(s.get("usage", {}).get("prompt_tokens", 0) for s in valid_scores)
    total_completion_tokens = sum(s.get("usage", {}).get("completion_tokens", 0) for s in valid_scores)

    print(f"\nTurns audited: {len(valid_scores)}/{len(TURNS)}")
    print(f"JSON parse rate: {json_parse_rate:.1%}")
    print(f"\n--- FACT QUALITY ---")
    print(f"Total facts extracted: {total_facts}")
    print(f"Total ground truth items: {total_gt}")
    print(f"Ground truth matched: {total_matched}/{total_gt} ({overall_accuracy:.1%})")
    print(f"Noise facts: {total_noise}/{total_facts} ({overall_noise:.1%})")
    print(f"Non-atomic facts: {total_non_atomic}/{total_facts} ({1-overall_atomicity:.1%})")
    print(f"Avg confidence: {sum(s['avg_confidence'] for s in valid_scores)/len(valid_scores):.2f}")

    print(f"\n--- EXTRACTION METADATA ---")
    total_decisions = sum(s["decisions"] for s in valid_scores)
    total_files = sum(s["files_touched"] for s in valid_scores)
    total_invalidated = sum(s["invalidated"] for s in valid_scores)
    print(f"Decisions extracted: {total_decisions}")
    print(f"Files touched: {total_files}")
    print(f"Invalidated facts: {total_invalidated}")

    print(f"\n--- COST ESTIMATE ---")
    print(f"Total prompt tokens: {total_prompt_tokens}")
    print(f"Total completion tokens: {total_completion_tokens}")
    # gpt-4.1-mini pricing: $0.40/1M input, $1.60/1M output
    cost = (total_prompt_tokens * 0.40 / 1_000_000) + (total_completion_tokens * 1.60 / 1_000_000)
    print(f"Estimated cost: ${cost:.4f} for {len(valid_scores)} turns")
    print(f"Cost per turn: ${cost/len(valid_scores):.4f}")

    print(f"\n--- GATE DECISION ---")
    gate_pass = True
    reasons = []

    if json_parse_rate < 0.95:
        gate_pass = False
        reasons.append(f"JSON parse rate {json_parse_rate:.1%} < 95% threshold")
    else:
        reasons.append(f"JSON parse rate {json_parse_rate:.1%} >= 95% [OK]")

    if overall_accuracy < 0.80:
        gate_pass = False
        reasons.append(f"Accuracy {overall_accuracy:.1%} < 80% threshold")
    else:
        reasons.append(f"Accuracy {overall_accuracy:.1%} >= 80% [OK]")

    if overall_noise > 0.10:
        gate_pass = False
        reasons.append(f"Noise ratio {overall_noise:.1%} > 10% threshold")
    else:
        reasons.append(f"Noise ratio {overall_noise:.1%} <= 10% [OK]")

    gate_status = "PASS" if gate_pass else "FAIL"
    print(f"Gate: {gate_status}")
    for r in reasons:
        print(f"  - {r}")

    # Save detailed results to JSON
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": settings.extractor_model,
        "turns": len(TURNS),
        "summary": {
            "json_parse_rate": json_parse_rate,
            "accuracy": overall_accuracy,
            "noise_ratio": overall_noise,
            "atomicity_rate": overall_atomicity,
            "total_facts": total_facts,
            "total_ground_truth": total_gt,
            "total_matched": total_matched,
            "total_noise": total_noise,
            "total_non_atomic": total_non_atomic,
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "estimated_cost_usd": cost,
            "cost_per_turn_usd": cost / len(valid_scores) if valid_scores else 0,
            "gate_pass": gate_pass,
        },
        "per_turn": [],
    }

    for result, score in zip(all_results, all_scores):
        turn_data = {
            "turn": score.get("turn", "?"),
            "raw_content": result.get("raw_content", ""),
            "parsed_facts": [f if isinstance(f, dict) else {"content": str(f)} for f in result.get("parsed", ExtractionResult(facts=[], files_touched=[], decisions=[], invalidated_fact_ids=[], turn_number=0)).facts],
            "ground_truth": result.get("ground_truth", []),
            "score": {k: v for k, v in score.items() if k != "usage"},
            "usage": score.get("usage", {}),
        }
        output["per_turn"].append(turn_data)

    output_path = Path("scripts/audit_results.json")
    output_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"\nDetailed results saved to: {output_path}")


if __name__ == "__main__":
    asyncio.run(run_audit())
