"""
DEMO — runs on real input from the product's own work.

Scenarios:
  A. Safe request: the worker ships a defective first proposal (allergen +
     unconfirmed ingredient). Guardrails/checkpoints feed structured corrections
     back; the worker's behavior changes across attempts until the plan is safe
     and ships.                                          [Pillars 1-4, Must #2]
  B. Unsafe stated calorie target: a non-auto-recoverable safety breach. The
     harness STOPS and asks a human instead of guessing. [Should #3, Pillar 4]
  C. Replay: re-run scenario A from a mid-pipeline checkpoint WITHOUT invoking
     the worker.                                         [Should #2]
  D. Bonus: drop a structurally different worker into the SAME pipeline with no
     harness changes.                                    [Should #1, Bonus]
"""
from harness import Harness, MockMealWorker, TemplateMealWorker, Outcome

LINE = "=" * 70


def banner(t):
    print("\n" + LINE + "\n" + t + "\n" + LINE)


def show(report):
    print(f"  worker          : {report.worker}")
    print(f"  attempts        : {report.attempts}")
    print(f"  outcome         : {report.outcome.value}")
    print("  checkpoints     :")
    for cp in report.checkpoints:
        print(f"      {cp['checkpoint_id']:16} {cp['status']:4}  | {cp['criteria']}")
    if report.alarms:
        print("  alarms          :")
        for a in report.alarms:
            print(f"      [{a['severity']:8}] {a['type']:26} -> {a['recommended_action']}")
    if report.escalation:
        print("  escalation      :")
        print(f"      reason   : {report.escalation['reason']}")
        print(f"      question : {report.escalation['question']}")


# ----- Scenario A: behavior change under guardrail/checkpoint feedback -----
banner("SCENARIO A  |  safe request, worker corrects itself under feedback")
h = Harness(workspace_root="runs", max_attempts=4)
report_a = h.run(MockMealWorker(), "inputs/weekly_request.json")
show(report_a)
run_a_id = report_a.run_id
if report_a.final_plan:
    shopping = sorted({i["name"][4:] for m in report_a.final_plan["meals"]
                       for i in m["ingredients"] if i["name"].startswith("buy:")})
    print(f"  shipped plan    : {len(report_a.final_plan['meals'])} meals, "
          f"shopping list = {shopping}")


# ----- Scenario B: unsafe target -> human-in-the-loop stop -----------------
banner("SCENARIO B  |  dangerously low calorie target, harness stops and asks")

def human_rejects(packet):
    # A real deployment would surface `packet` to a person/professional.
    print("  >> harness paused and handed this to a human:")
    print(f"     {packet.reason}")
    for a in packet.blocking_alarms:
        print(f"     blocking alarm: {a['type']} {a['context']}")
    print("  >> human decision: REJECT (do not ship an unsafe target)")
    return False   # True=override+ship, False=reject, None=halt pending review

h2 = Harness(workspace_root="runs", max_attempts=4, human_gate=human_rejects)
report_b = h2.run(MockMealWorker(), "inputs/unsafe_target_request.json")
show(report_b)


# ----- Scenario C: replay from a checkpoint without re-running the worker ---
banner("SCENARIO C  |  replay scenario A from CP3_FIT forward (no worker call)")
replayed = h.replay_from(run_a_id, "CP3_FIT")
for cp in replayed:
    print(f"      {cp['checkpoint_id']:16} {cp['status']:4}  (replayed from persisted snapshot)")


# ----- Scenario D (BONUS): swap in a different worker, no harness changes ----
banner("SCENARIO D (BONUS)  |  swap TemplateMealWorker into the same harness")
report_d = h.run(TemplateMealWorker(), "inputs/weekly_request.json")
show(report_d)

print("\n" + LINE)
print("SUMMARY")
print(LINE)
print(f"  A mock worker     : {report_a.outcome.value}  (corrected over {report_a.attempts} attempts)")
print(f"  B unsafe target   : {report_b.outcome.value}")
print(f"  C replay          : {[c['status'] for c in replayed]}")
print(f"  D template worker : {report_d.outcome.value}  (no harness code changed)")
