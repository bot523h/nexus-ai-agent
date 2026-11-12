# AETHEL director — doctrine (Wave 1)

**Status:** EXPERIMENTAL advisory layer. Owns no project state; renders nothing;
dispatches no command; mints no passport.

## 1. Fountain AST

A bounded recursive-descent parser (`fountain_parser.py`) produces a typed AST:
scene headings, action, character cues, parentheticals, dialogue (with a
syllable-based timing *estimate*), transitions, `#` sections, `=` synopses,
`[[notes]]`, and page breaks. Every element carries a line number. Bounds:
`MAX_SCREENPLAY_LINES`, `MAX_SCREENPLAY_CHARS`, `MAX_SCENES`,
`MAX_BLOCKS_PER_SCENE`. The syllable estimate (`0.25 s`/syllable) is a planning
heuristic, never a claim about a performance.

## 2. ATLAS bridge

`compile_fountain_to_story_document(script, episode_id)` emits a versioned ATLAS
`StoryDocument`:

* each character's first dialogue in a scene -> `ENTER_LOCATION` at the scene's
  location;
* documented action patterns -> `TRANSFER_PROP` (`X hands the Y to Z`) and
  `SET_STATE` (`X takes the Y`);
* scene order -> explicit `scene:<id>` temporal anchors with a 1-tick minimum
  gap, so ATLAS `check_consistency` proves the ordering;
* one `StoryBeat` per scene.

Anything the patterns do not match is **not** emitted: the causal engine then
reports `UNKNOWN`, never a fabricated `PASS`.

## 3. Shot planner

For the primary pair `(A, B)`, the action line is
`L_AB = p_B - p_A`. A camera is on side `s in {-1, +1}` when
`sgn((p_cam - p_A) x L_AB) == s`. `plan_shot_sequence` lays out
Establishing -> Master -> OTS(A) -> OTS(B) -> CU(A) -> Insert(B), all on one
declared side. The 30-degree rule (`>= 30 deg` or `>= 25%` focal change between
consecutive same-subject shots), eyeline match, and Hall proxemics bands
(intimate `<0.45`, personal `<1.2`, social `<=3.6`, public `>3.6` m) with a
`0.35 m` minimum separation are enforced; violations raise typed errors.

## 4. Determinism

All functions are pure: identical inputs give identical ASTs, documents and shot
sequences. No ambient `Path.cwd()`, no network, no randomness.
