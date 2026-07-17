# Prompting

Turn the user's request into a compact visual brief. These fields guide shaping; they are not questions to ask unless a missing fact makes the request unsafe or impossible.

```text
Goal: <what the finished image must accomplish>
Subject: <primary subject and material attributes>
Scene: <environment or background>
Style: <photo, illustration, product render, poster, etc.>
Composition: <orientation, framing, camera, subject placement>
Lighting/mood: <only when useful>
Text: "<exact requested wording>"
Change: <edit-only change>
Preserve: <edit-only invariants>
Avoid: <artifacts or forbidden changes>
```

## Taxonomy

- `photorealistic-natural`: real materials, plausible anatomy, natural light, restrained retouching.
- `product-mockup`: clear product geometry, readable packaging, controlled background, clean edges.
- `ads-marketing`: one focal message, deliberate crop, space for exact copy, no invented claims.
- `illustration-story`: stated medium, central action, coherent setting, no extra characters.
- `identity-preserve`: an edit that keeps a supplied person, object, or brand asset recognizably unchanged.
- `precise-object-edit`: change one named object or region while locking the rest.
- `background-extraction`: isolate the stated subject for post-processing with a chroma-key plan when alpha is needed.
- `compositing`: combine role-specific sources with coherent scale, perspective, lighting, contact shadows, and occlusion.

## Exact text and scope

- Copy requested in-image text exactly, including capitalization, punctuation, and spelling. Put it in the `Text` field in quotes.
- Preserve a detailed request rather than embellishing it. For a short request, add only conventional details that support the stated outcome.
- Do not invent brands, slogans, characters, objects, palettes, claims, or story elements.
- Keep text short and place it only when the user requests in-image text. Never silently replace requested text with similar wording.

## Edit roles and invariants

Inspect every input before writing an edit prompt. State its role and order explicitly: `edit target`, `insert/reference`, `style reference`, or `scene reference`.

For a selective edit, name both sides:

```text
Change only: <requested region or property>.
Preserve exactly: <identity, pose, product shape, logo, text, camera, colors, and all other named invariants>.
Do not crop, redesign, replace, or restyle preserved elements.
```

For a composite, specify which source supplies each element. Supporting references must not silently replace the edit target.

## Result review

Review each output against the brief: subject accuracy, composition, crop, requested text, hands/faces when relevant, product geometry, source identity, and preserved invariants. For a cutout, also inspect alpha edges and color spill. A weak result is not permission for another paid submit.
