# Robustness studies (beta)

One nominal simulation says a design works once, exactly as drawn. A robustness study runs the revision's own pick and
place many times, each run in a slightly different world drawn from the project's tolerances (the object a few
millimetres off its mark and turned, a heavier egg, softer pads, a weaker servo, a slower host), and reports how often
it still succeeds, what the failures have in common, and whether each failure survives other simulator settings.

**It is simulation, for the tolerances shown; not a field failure rate.** The ranges are estimates (each one says where
it comes from), the physics is MuJoCo's, and the plan is the one the Studio simulated. A study tells you where to look
and what to test on the real robot; it doesn't certify anything.

## Run one

**In the Studio:** open the Simulation tab with *Pick and place* selected. The **Robustness** card under its result runs
a study of the revision on screen: choose 50, 200 or 1,000 runs, or a *Stress test* (every range doubled), and press
*Run a study*. The card shows the run it is on, the success rate so far with its interval, the failures so far by kind,
and the time left (estimated). *Cancel* stops it within a couple of seconds; the finished runs stay, and *Carry on* (or
the same settings again) continues from them. *Run N more* deepens a finished study: the runs already made are kept, and
the new ones are the ones a bigger study would have made. *Details* lists every failure group, what matters most, every
tolerance with its source, and the project's studies.

**In a terminal:**

```bash
morph study egg-picker                     # 200 runs and the solver checks: about 2 minutes on 4 cores
morph study egg-picker --runs 1000         # deeper: the first 200 are kept
morph study egg-picker --stress            # every tolerance range doubled
morph study egg-picker --rev r003 --seed 7 --timestep 2 --workers 2 --no-checks
```

Ctrl+C keeps the finished runs and the same command carries on. It prints the result as plain text and where it is
saved; it exits 0 when the study finished, 130 when it was stopped and 1 when it couldn't run.

A study needs a finished revision of a whole robot (a Morph Studio project, not a gripper-only project from the earlier
command-line prototype) with a pick-and-place simulation. A revision whose simulation stage failed, a preview-only build
or one without pick and place is refused at once, with the reason.

## Tolerances and where they come from

Every run draws one value from each range. Defaults for the egg-picker (other objects and servo families get ranges
relative to their own values, marked "assumed"):

| Tolerance | Default | Source |
|---|---|---|
| Object off its mark, across the jaws / along its length | +/-8 mm each | assumed: the object is set down by hand |
| Object turned on the table | +/-15 deg | assumed |
| Object size | 92-108 % | USDA egg weight classes, medium to jumbo |
| Object mass | 50-72 g | USDA egg weight classes, medium (50 g) to jumbo (72 g) |
| Pad friction | 70-130 % | assumed |
| Grip-force calibration | 75-125 % | FSR 400 datasheet: +/-6 % part to part, +10 % hysteresis |
| Servo torque and gain | 70-100 % | ST3215: 16.5 kg-cm at 6 V vs 19.5 at 7.4 V |
| Servo deadband and backlash (half-width) | 0-0.5 deg | ST3215: backlash 0.5 deg or less |
| Encoder noise | 0-1.5 counts | 4096 counts per turn (ST3215) |
| Host set-point rate | 30-200 Hz, held between updates | assumed |
| World mode: robot mount off in x / y; turned | +/-10 mm each; +/-2 deg | assumed |
| World mode: each box off its measured place | 0-15 mm, in a random direction | assumed |

Editors change a range in *Details* (or `POST /api/study/tolerances`): finite numbers within each tolerance's limits,
the low end not above the high end, *Vary* on or off. A changed spec marks the studies made with the old one
**outdated**; *Back to the defaults* undoes every edit. In World mode, **Only the room varies** keeps the object and the
hardware nominal and varies only the mount and the boxes, so every failure is the room's. A stress test doubles every
range about its nominal value (the set-point rate's low end is halved), never below a floor: half the object's size, a
tenth of its mass, friction, grip force or servo torque, 5 Hz.

Backlash is modelled as a deadband on the servo's position error, the host's set-points are held between updates, and
the plan, the gains and the targets stay nominal: the host doesn't know about the error it is running into.

## How to read the result

**The headline:** "Succeeds in 140 of 200 randomized runs (95% CI 63-76%); the nominal run succeeds." The interval is
Wilson's 95 % score interval, rounded outward so the interval shown always contains the computed one. When every run
succeeds, or none does, it is the exact (Clopper-Pearson) bound: "Succeeds in all 200 randomized runs (at least 98.1%
with 95% confidence)". Few runs give wide intervals, and the card says so with numbers: 3 of 4 is 30-96 %, all of 6 is
"at least 54.0%". Run more before you trust a difference between two designs whose intervals overlap.

**Failure groups:** each failed run is labelled by what went wrong: the jaws stayed shut at release and carried the
object away, dragged off the target, missed the grasp, dropped on the way, squeezed harder than the object can take, out
of reach, or hit a box in the room during a named phase. Each group shows its share with its own interval and "Most
likely when": the one tolerance range that best separates that group's runs from the rest (Youden's J over quantile cuts
of each value, or of its distance from nominal), as "egg turned 3.4-9.4 deg: 49% fail this way vs 13% otherwise". A rule
is compared with what the same search finds on shuffled outcomes (the 95th percentile of 60 shuffles); below that it
says "(could be chance)". A group that holds every counted run gets no rule: there is nothing left to separate it from.
*Replay run N* plays the group's run closest to nominal in the 3D view.

**Runs that don't count:** a run in which MuJoCo reported a bad position, velocity, acceleration or control (it restarts
the run when that happens), a full contact buffer or a non-finite state is *numerically unstable*; a run that raised an
error is listed too. Neither is counted, and the card says how many. When no run could be counted it says exactly that,
never the "One nominal run" note.

**Flags on the card:**
- *Outdated*: the tolerances changed after the study. Its numbers are for the ranges it ran with.
- *Stopped after N of M runs*: cancelled, or the Studio stopped; the numbers are for the runs it finished. Runs still in
  flight when it stopped are slightly more often the slow ones (a jam takes about 15 % more computing than a clean
  place), so a partial rate can lean optimistic by up to about one run per worker process.
- *Its revision was replaced*: Reset demo (or a deleted revision) reused the name for another design. The study is not
  of what that revision is now; it can't be carried on or replayed.
- *Simulated with other Morph code*: the robot rebuilt for the study differs from the one the revision was simulated
  with.

## Model-sensitive results

A failure the simulator produces is not always one the robot will have. After the runs, each failure group's runs (at
most 20) and 15 successful runs are run again at half the timestep and with softer friction (MuJoCo's `impratio=1`). A
group is **model-sensitive** when, under either setting, fewer than 80 % of its runs still fail the same way, or at
least 20 % (and at least 2) of the successful runs now fail that way. The card then says: "This result changes with the
simulator's friction or timestep settings. Test it on the real robot." Otherwise it says the failure is the same under
other solver settings.

The same check covers the successes: when at least 20 % (and 2) of the re-run successful runs fail under either setting,
the card flags that the success rate itself depends on the simulator's settings. A study in which nothing failed can be
as model-sensitive as one in which something did.

The default egg-picker is the example: 58 of its 60 failures are the jaws staying shut at release with the egg turned
3-9 degrees. At half the timestep 16 of 20 re-runs still jam; with `impratio=1` only 1 of 20 does. Whether the real
gripper jams there needs a bench test: turn an egg 5 degrees on its mark and release it.

## Reproducible, resumable

Run *i* of a study draws its world from the study's seed and *i* alone (`SeedSequence([seed, i])`), and every range
draws even when it is switched off. So the outcome of a run doesn't depend on how many worker processes ran it or in
what order, a deeper study extends a shallower one, and editing one range never moves the others. The same revision,
design, Morph code, tolerances, seed, timestep and mode always make the same study, which is why the same settings carry
on instead of starting over.

**When the Studio stops in the middle:** Ctrl+C in its terminal cancels the study and keeps its runs. When the Studio's
process is killed (a crash, a closed window, `kill`), the study's processes stop on their own within about two seconds
(measured on Linux), and the next Studio shows it as "Stopped when the Studio stopped after N of M runs" with the runs
it saved; *Carry on* finishes it with the same runs as if it had never stopped. A study can't be carried on once its
revision was replaced or Morph's simulation code changed (carrying on would mix two designs or two versions in one
study): run a new one.

## Bringing your own compute (`--remote`)

Studies run on the computer that runs the Studio, in their own processes beside the build queue: a build never waits for
a study. Start the Studio on a machine with more cores and everyone's browser uses them:

```bash
morph studio --remote                 # prints a link with an access code for the other computers
morph studio --team --remote          # or a team Studio: viewers read studies, editors run them
```

The card names that computer and estimates the time from the project's own measured speed. A run takes about one second
of computing at 1 ms steps: 200 runs and their solver checks (twice up to 20 runs of each failure group and 15
successes) take about two minutes on 4 cores.

- **Worker processes:** the cores the Studio's process may use, minus one (a container's or `taskset`'s share counts,
  not the whole machine's); `MORPH_STUDY_WORKERS` sets another number, never more than those cores. They run at a lower
  priority (`MORPH_STUDY_NICE`, default 5; below normal on Windows). Each takes about 150 MB and starts in about half a
  second on an idle machine (about a second when the machine is busy).
- **One study per project at a time**, also across processes (a study from the terminal and one from the Studio);
  studies of different projects take turns (`MORPH_HEAVY_JOBS`, default 1 at a time).
- **Sizes:** from the Studio at most 10,000 one-millisecond runs' worth (5,000 at 0.5 ms, 20,000 at 2 ms); `morph study`
  on that computer takes up to 100,000.
- **Replays** run one at a time in a study process of their own; a few may wait for their turn, and more are asked to
  try again in a moment. The last eight are kept.

For scripts that call `morph.services.robust` directly: worker processes are started with the *spawn* method on every
operating system, so each one imports your script's main module again. Guard the script's work with `if __name__ ==
"__main__":`.

## Measured (0.2.0b1)

On a 4-core Xeon at 2.8 GHz, offline, with the code of this release: the studies with nothing else running (load
under 2 at the start), the cancel, restart and build timings beside other work (load 3 to 17).

| What | Measured |
|---|---|
| The default egg-picker, 200 runs at 1 ms (seed 1) | 140 of 200 succeed (Wilson 63.3-75.9 %). 58 jams (23.2-35.6 %): "egg turned 3.4-9.4 deg: 49% fail this way vs 13% otherwise", model-sensitive (16 of 20 still jam at half the timestep, 1 of 20 with `impratio=1`); 2 drags |
| Speed at 1 ms | 2.7 runs/s on 3 worker processes; 0.9 on 1, 1.6-1.8 on 2, 2.3-3.3 on 4. 200 runs and their 74 check runs: about 2 minutes (117-119 s) |
| First result | 5.6-6.1 s after the start (rebuilding the robot and its plan takes 3.7-3.9 s of it) |
| Worker processes | Start in 0.3-0.4 s at 135 MB from a script; 0.7-0.8 s at 150 MB under `morph studio`, whose module each one imports again |
| Cancel | 0.06-0.25 s in the engine at any moment of a start (rebuilding, loading, running); 0.55 s from the Studio's request to the study's end. No study process left |
| A killed Studio | Its study processes end 1.4-1.8 s later; restarted, the study reads "Stopped when the Studio stopped after 8 of 40 runs" at once, and Carry on gives the same 40 runs as a study that never stopped |
| Reproducibility | The same 40 runs at 1 ms on 1, 2 and 4 worker processes in batches of 2, 4 and 8, and through `morph study` on 1, 2 and 4 |
| Builds beside studies | A build during two studies starts at once and takes 20.3 s (21.5 s with no study running) |
| Room only: a carton wall 12 mm from the open jaws | 185 of 200 succeed (Wilson 88.0-95.4 %); 15 hit it during the release (4.6-12.0 %): "mount 4.5 to 10.0 mm off in y: 20% fail this way vs 3% otherwise", the same under other solver settings (15 of 15). 1.3 runs/s on 3 workers: World mode tests every collision pair |
| Not measured | 1,000 runs on a 16-core `--remote` computer (none here): about 70 s on 15 workers if it scales as it does on 4 cores |

## Limits

- Pick and place only: the payload sweep, the workspace and the shake test are not studied.
- The plan is the nominal one: a run that starts wrong is not re-planned, which is what a robot without sensing does.
- One-parameter rules only. Two tolerances that cause a failure together show up as two weaker rules (see *What matters
  most* in Details); there are no interaction or variance-based sensitivity indices yet.
- The solver checks re-run at most 20 runs of a group and 15 successes, not every run.
- No GPU: MuJoCo on the CPU. Tested on Linux; the processes are started the same way on Windows and macOS.

## Where it is kept

`<project>/.studies/`: `tolerances.json` (the project's edits), `active.json` (the running study's lock and heartbeat)
and one folder per study with `study.json` (its definition, status, timing and summary), `runs.jsonl` and `checks.jsonl`
(one line per run, about half a kilobyte). A study never changes a revision or a stage hash. The folder is never
exported, a fork starts without it, and a deleted project takes it to the trash. Nothing about a study leaves the
computer.
