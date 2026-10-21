---
name: ui_design
version: 1.0.0
type: instruction
os: any
permissions: []
description: "Design or change anything people look at (app UI, widget, mockup, HTML page or report, chart, icon) so it fits the existing system, uses real content and states, and is checked by eye and by measurement at the viewer's real size."
when_to_use: "Before designing or changing how something looks, judging screenshots of such work, or briefing another model that will draw it. Not software architecture."
model_experience:
  what_model_sees: "A few lines of metadata while enabled. On read: a one-screen playbook; references hold the craft floor, the render-and-measure recipe and two browser-side snippets (measure.js, freeze.js). No tools."
  token_effect: "A few context lines while enabled. The body costs about two thousand tokens when read; each reference costs tokens only when opened."
---

# Designing something people will look at

A surface looks designed when every choice on it is explained by what the viewer does there. This is a pattern, not a law: the request, the product's design document and the owner's words outrank it.

## 1. The job first

- Name the viewer, their measured window (size, theme, engine, saved panel widths; the runtime context's `owner_client` fact names the owner's surface) and the one thing they do here.
- Authority, in order: the human's words; the product's design document and tokens (for Ouroboros's own interface `docs/DESIGN.md` and `web/ui.css`); the screens beside yours; this playbook; your habits.
- Census before invention: find every existing rendering of the same intent; reuse it or replace all of them. One intent, one control.
- Real content at its worst real length: the longest title, the long list, the empty list, the error. Real data from history exports and task results is allowed and preferred; only side effects on the live API are off limits. Invented data is at least as awkward, and you say so.
- A step from today unless a redesign is asked: change structure and behaviour, keep the materials people already approved. Recolouring the same boxes is not a design.

## 2. Composition essentials

- One thing first: the primary object or action is found without reading; the rest is quieter by size, weight or position.
- Shared edges: related things start on one left edge and sit on one baseline; correct by eye after looking.
- Spacing rhythm: one small scale, tight inside a group, generous between groups. Air is space, never faded text.
- Proximity before boxes: a border, fill or shadow marks a separate object; if everything has one, nothing does.
- Quiet surface: caveats, counters and test counts go in your reply, never on the surface.
- A plan that would look the same for another subject is a default, not a choice. The checkable floor, the tells and the words: `references/craft.md`.

## 3. Look first, then measure

- Open the delivered thing as its viewer will: the entry file or page, alone, at their size, theme and engine. Look at the first screen at 1:1, then at 2× crops of what changed; never a stitched sheet or a scaled grid.
- Then measure what eyes miss: container overflow, clipped or covered text, bars over content, stuck controls, small targets, contrast (`references/browser.md`, `references/measure.js`). Measure after a paint; WebKit can report one width before the first paint and another after.
- Findings are evidence, never a verdict. A green test, a saved screenshot or a check count is not a look. If you could not render or see the result, say so and give what you measured. Fix what the look shows, look again, and stop when the frame is right.

## 4. Delegating the drawing

A delegated session may not see this skill: give it the absolute paths of this file and of `design_demo`, and say reading them is allowed. The brief template is `brief.md` in the `design_demo` skill's references; its first paragraph is what the viewer does, coverage comes after.

## 5. The owner's taste

- Craft is general; taste is personal and changes. Before designing for a known owner, `knowledge_list(scope="global")`, then `knowledge_read(topic, scope="global")` for the topic holding their design reactions, if any.
- After a reaction, `knowledge_write(scope="global")`: the date, what was shown, their words verbatim, where it applies and where not, told, observed or inferred. Revise in place; a correction is evidence about that moment, not a standing rule until they make it one.
- Delegates return observations; only the mind writes memory. A taste note never becomes product law or part of this skill.

## Done means

A delivered page opens alone and fetches nothing; zero console errors; the awkward content fits; you looked at it at the viewer's size, in both themes when the product has two, and can name what you saw and what you measured.
