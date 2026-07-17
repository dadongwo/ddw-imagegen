# Sample Prompts

Use the shared brief fields below. Replace only bracketed user details; keep `Text` exactly as supplied and do not add brands or subjects.

## photorealistic-natural

```text
Goal: A natural-looking editorial image of [subject].
Subject: [subject and visible attributes].
Scene: [realistic location].
Style: Photorealistic editorial photography.
Composition: [orientation and framing].
Lighting/mood: Soft natural daylight.
Text: "[exact requested text]"
Change: None.
Preserve: [named user requirements].
Avoid: Plastic skin, extra people, distorted anatomy, invented objects.
```

## product-mockup

```text
Goal: A clear mockup presenting [product] for review.
Subject: [product] with its supplied packaging and markings.
Scene: A clean [requested] surface.
Style: Photorealistic product mockup.
Composition: [orientation], product centered with readable front face.
Lighting/mood: Soft studio lighting.
Text: "[exact requested text]"
Change: None.
Preserve: Product shape, supplied logo, colors, and text.
Avoid: Invented branding, warped packaging, extra products.
```

## ads-marketing

```text
Goal: A focused marketing image for [subject].
Subject: [subject].
Scene: [requested setting].
Style: Polished advertising photography.
Composition: [orientation], leave intentional space for copy.
Lighting/mood: [requested mood].
Text: "[exact requested text]"
Change: None.
Preserve: [supplied product details or requirements].
Avoid: Invented claims, slogans, brands, or extra subjects.
```

## illustration-story

```text
Goal: Illustrate [stated action or moment].
Subject: [named subject].
Scene: [requested setting].
Style: [requested illustration medium].
Composition: [orientation and focal placement].
Lighting/mood: [requested mood].
Text: "[exact requested text]"
Change: None.
Preserve: [named visual requirements].
Avoid: Extra characters, extra plot elements, invented text.
```

## identity-preserve

```text
Goal: Update the supplied image without changing the subject's identity.
Subject: Edit target image, [named person or object].
Scene: [requested resulting scene].
Style: Match the target image unless the user requests another style.
Composition: Preserve the target framing and camera.
Lighting/mood: Match the target unless requested otherwise.
Text: "[exact requested text]"
Change: [requested change only].
Preserve: Identity, pose, facial features, product geometry, logo, and all untouched areas exactly.
Avoid: Face replacement, crop changes, restyling, or added subjects.
```

## precise-object-edit

```text
Goal: Make one precise change to the supplied image.
Subject: Edit target image; [named object or region].
Scene: Preserve the existing scene.
Style: Match the target image.
Composition: Preserve crop, camera, and placement.
Lighting/mood: Preserve existing lighting.
Text: "[exact requested text]"
Change: Change only [object or region] to [requested result].
Preserve: Everything outside [object or region] exactly, including identity, text, logo, and product shape.
Avoid: Reframing, replacement of other objects, or unrelated restyling.
```

## background-extraction

Use this recipe only after the transparency preflight in `SKILL.md`. Simple opaque subjects proceed directly. For hair, fur, feathers, smoke, glass, liquids, translucent materials, reflective edges, or soft shadows, stop before the paid submit and ask whether the user accepts a chroma-key approximation.

```text
Goal: Isolate [subject] for a transparent project asset.
Subject: [subject] from the supplied target or requested generation.
Scene: Flat chroma-key [key color] background with no shadows beyond the subject edge.
Style: Match the target image or [requested style].
Composition: [orientation], subject fully inside the frame.
Lighting/mood: Even edge-defining light.
Text: "[exact requested text]"
Change: Remove the original background only.
Preserve: Subject identity, shape, fine details, and requested text exactly.
Avoid: Key-colored subject details, clipped hair or glass, new objects, or a textured background.
```

## compositing

```text
Goal: Combine the supplied sources into one coherent image.
Subject: Edit target [source A]; insert/reference [source B].
Scene: [requested scene reference or setting].
Style: Match [named target or requested style].
Composition: Place [source B] at [requested location] in [orientation].
Lighting/mood: Match direction, intensity, and color temperature across sources.
Text: "[exact requested text]"
Change: Add [source B] to [source A] only.
Preserve: Edit target identity, framing, product details, and all named invariants exactly.
Avoid: Replacing the edit target, mismatched scale or perspective, extra subjects, or invented text.
```
