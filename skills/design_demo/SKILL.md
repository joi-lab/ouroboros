---
name: design_demo
version: 1.0.0
type: instruction
os: any
permissions: []
description: "Put a visual choice before a person as one self-contained page: one scene at their real size, variants swapped in place, A recommended, short plain questions; then record their reaction."
when_to_use: "When a visual change needs the owner's choice or approval (mockups, variants, before/after, redesign, palette, icon), or a look would otherwise be approved by words alone. Read ui_design too."
model_experience:
  what_model_sees: "A few lines of metadata while enabled. On read: a one-screen playbook, an author brief template (references/brief.md) and a one-file comparison page template (assets/compare.html). No tools."
  token_effect: "A few context lines while enabled. The body costs about two thousand tokens when read; the brief and the template cost tokens only when opened."
---

# Showing a visual choice

Write this sentence first: "On this page you compare X and decide Y." It becomes the page title and the first line of any brief. The person will spend a minute: a gallery of every state is an archive and a folder of screenshots is evidence; neither is a demo.

A change to an approved look is decided on renders of both versions, never on a sentence. If a variant moves away from something the owner already approved, show it and say so.

## The page

- One self-contained file: the chat's Open copies only that file, so no server, command, sibling asset or URL parameter. `assets/compare.html` is the template; frames go inline.
- One scene at a time: Now and the variants, swapped in place by the strip or keys 1–9 and arrows. No fade, no reload; scroll kept, the compared element anchored. A blink between two pictures hides the difference being judged.
- Real size, 1:1, at the viewer's window. Never shrink windows to fit several; side by side is a second view showing the compared element at 1:1.
- Real product code and the real long data (ui_design §1).
- One plain line per variant: what changes, what it costs. A is the recommendation, chosen for fit over novelty. Always "none of these, here is what bothers me".
- Demo chrome stays outside the product frame; nothing floats over the app. Limitations go in the message, not on the page, unless they change the decision.

## The variants

- Two to four genuinely different directions, only when the task is a choice; a refinement shows Now and one proposal. Each is a step from today, named by what the viewer sees change.
- One direction per parallel designer; a set that converged on one composition is rejected before anyone sees it.
- A model critic may drop clearly worse or broken variants; close calls go to the owner.
- A motion variant plays by itself and loops, respecting reduced motion. When the template is the wrong form (motion, a flow), keep its invariants: one file, real size, in-place switch, A recommended, questions on the page.

## Before sending

Open the file alone at the viewer's size and engine. Switch every variant, look at the first screen, then measure (ui_design §3). Fix what that shows, look again, and send when the page is right. A critic gets the same file and the viewer sentence, never contact sheets.

## The message

One line saying what is decided; the file through `send_file` plus one inline first-screen image for phone and Telegram readers; at most three plain questions, numbered across rounds, A first with its cost, then B, then room for none. Written for the viewer in their words: no file names, test counts or process story; if something could not be verified, one sentence says so.

## After the answer

Record the reaction (ui_design §5): date, what was shown, their words, scope. Close the loop item by item: what changed, what deliberately did not and why, what happens next. The chosen variant's code and data start the implementation; do not rebuild it from a description.
