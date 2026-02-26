# Specification Enrichment Report

**Mode:** Spec Review (Collaborative Ideation)
**Input:** `/home/kelleyb/Desktop/testing.sample/boardfoot.sample.plan.md`
**Project:** board-foot
**Date:** 2026-02-25T21:09:55.859893+00:00
**Review ID:** `20260225T210955_75c5a9_review`
**Reviewer Models:** grok-4-0709, gpt-5.2
**Dedup Model:** minimax-m2.5
**Total Cost:** $0.0910

## Summary

- **Total Suggestions:** 38
- **Suggestion Groups:** 27
- **Multi-Reviewer Consensus:** 10
- **Single Source:** 17

## Accessibility

### Screen Reader and Keyboard Navigation Optimization: Enhance accessibility with content descriptions and hints that inclu -- 2/2 reviewers

- **grok-4-0709:** Screen reader optimized labels: Enhance TextInputLayout with content descriptions and hints that include unit expectations (e.g., "Enter length in inches"), ensuring screen readers announce full context for visually impaired users, improving inclusivity without altering the core layout.
  - *Context:* Relates to 3.10 activity_main.xml input fields
- **gpt-5.2:** Improve keyboard navigation and IME actions: Set explicit IME actions (Next/Done), move focus predictably across fields, and ensure TalkBack labels read well (e.g., “Thickness in inches”). Also consider larger tap targets and a high-contrast theme option for shop environments with glare/dust.
  - *Context:* TextInputEditText configuration + overall theme

### Voice Command Input Support: Integrate Android's speech-to-text for dictating dimensions and price (e.g., "length twelve

- **grok-4-0709:** Voice command input support: Integrate Android's speech-to-text for dictating dimensions and price (e.g., "length twelve width six thickness one"), making the app hands-free for users in workshops where typing is impractical.
  - *Context:* Relates to 3.10 activity_main.xml input fields

## Content

### Educational Board Foot Explainer: Add an info icon linking to a dialog explaining the board foot formula and examples (e

- **grok-4-0709:** Educational board foot explainer: Add an info icon linking to a dialog explaining the board foot formula and examples (e.g., "A board foot is 144 cubic inches of wood"), enriching the app with value for beginners and positioning it as an educational tool.
  - *Context:* General, as a new content layer atop the core logic

## Data Model

### Fractional Input Parsing and Quick-Pick Chips: Extend dimension parsing to handle fractional inputs like "1 1/2" or "1.5 -- 2/2 reviewers

- **grok-4-0709:** Support fractional input parsing: Extend dimension parsing to handle fractional inputs like "1 1/2" or "1.5" interchangeably, storing them as decimals in the model, which accommodates users accustomed to carpentry notations and reduces entry errors.
  - *Context:* Relates to 3.11 MainActivity.kt parse logic for dimensions
- **gpt-5.2:** Add fractional inch quick-pick chips: Offer optional chips/buttons for common fractions (1/8, 1/4, 3/8, 1/2, 5/8, 3/4) that append to the current dimension field or set thickness quickly. This matches workshop reality where thickness is frequently a standard fraction, reducing typing friction.
  - *Context:* Dimensions inputs UI (especially thickness)

### Line-Item List with Undo/Remove: Introduce a simple list of calculated line items (each with dimensions, quantity, bf, c

- **gpt-5.2:** Line-item list with undo/remove: Introduce a simple list of calculated line items (each with dimensions, quantity, bf, cost) beneath the result, with swipe-to-delete and a one-tap Undo snackbar. Running totals become auditable and correctable, which is valuable when entering many boards quickly.
  - *Context:* Result/Total area and accumulation model (from pure totals to line items)

### Save Named Price Presets Per Species/Supplier: Let users save multiple named price presets (e.g., "Walnut S4S $12.50/bf"

- **gpt-5.2:** Save named price presets per species/supplier: Let users save multiple named price presets (e.g., “Walnut S4S $12.50/bf”, “Pine rough $3.25/bf”) and switch quickly. Real pricing varies by wood species, grade, and supplier; presets reduce repetitive entry and errors.
  - *Context:* Price input + state management

## Features

### Unit Conversion and Selection: Add unit selectors with common presets for dimensions (inches, feet+inches, mm) and price -- 2/2 reviewers

- **grok-4-0709:** Unit conversion selector: Add a dropdown or toggle for dimension units (inches, feet, cm, mm) that auto-converts inputs to inches for the board foot formula, allowing users in different regions or with varied measurement preferences to input comfortably without manual conversions, enhancing global usability.
  - *Context:* Relates to 3.11 MainActivity.kt calculate() logic
- **gpt-5.2:** Add unit selectors with common presets: Add a compact unit selector for dimensions (inches, feet+inches, millimeters) and price basis (per board foot, per cubic foot/meter, per piece). Users often think in different units depending on region and lumber type; unit switching reduces mental math and expands appeal without changing the core workflow.
  - *Context:* activity_main.xml inputs + MainActivity.calculate()
- **gpt-5.2:** Support feet-and-inches entry mode: Provide an optional input mode for length/width/thickness like `8' 6 1/2"` or separate fields (feet + inches). Woodworking contexts commonly use imperial fractional measurements; supporting this directly reduces errors and speeds entry.
  - *Context:* Dimensions row + parsing in MainActivity.calculate()

### Waste Factor Adjustment and Other Toggles: Include a slider or toggles to apply waste percentage (e.g., +10% for cuts),  -- 2/2 reviewers

- **grok-4-0709:** Waste factor adjustment slider: Include a slider to apply a waste percentage (e.g., +10% for cuts), automatically adjusting totals, which helps users plan for real-world material losses and provides more accurate project estimates.
  - *Context:* Relates to 3.11 MainActivity.kt total accumulation
- **gpt-5.2:** Add tax, waste, and discount toggles: Add optional adjustments: sales tax %, waste/overage % (e.g., +10% for defects), and discount % (e.g., contractor pricing). Lumber purchasing frequently includes these factors; exposing them as simple toggles makes totals more “real-world accurate” while keeping defaults off for simplicity.
  - *Context:* Totals section + calculation logic

### Preset Lumber Size Buttons: Include quick-select buttons for common lumber sizes (e.g., 2x4, 1x6) that auto-fill dimensi

- **grok-4-0709:** Preset lumber size buttons: Include quick-select buttons for common sizes (e.g., 2x4, 1x6) that auto-fill dimensions, speeding up repetitive calculations for standard boards and adding convenience for frequent users like builders.
  - *Context:* Relates to 3.10 activity_main.xml dimensions row

### Quantity Multiplier Per Line Item: Add a Quantity field (default 1) so users can price multiple identical boards in one 

- **gpt-5.2:** Quantity multiplier per line item: Add a Quantity field (default 1) so users can price multiple identical boards in one calculation. This is a highly common workflow (e.g., “8 boards at 96 x 6 x 1”) and complements the running total behavior.
  - *Context:* activity_main.xml + MainActivity.calculate() accumulation

## Integrations

### Export Data to Spreadsheet/CSV: Include a button to export running totals and individual calculations as a CSV file shar -- 2/2 reviewers

- **grok-4-0709:** Export totals to spreadsheet apps: Include a button to export the running total and individual calculations as a CSV file shareable via Android's share sheet (e.g., to Google Sheets or email), enabling users like contractors to integrate with their invoicing or inventory tools for seamless workflow continuation.
  - *Context:* Relates to 3.11 MainActivity.kt totalBoardFeet and totalCost state
- **gpt-5.2:** Export line items to CSV/PDF: Add an export option that generates CSV (for spreadsheets) and/or a simple PDF “cut list / purchase list” including date, supplier, price, and line items. This differentiates from basic calculators and fits contractor/pro workflows.
  - *Context:* General (new capability building on calculations)

### Live Lumber Price API Fetch: Integrate with a public API (e.g., for current wood prices) to auto-populate or suggest the

- **grok-4-0709:** Live lumber price API fetch: Integrate with a public API (e.g., for current wood prices) to auto-populate or suggest the price field based on wood type selection, providing real-time market insights and saving users research time.
  - *Context:* Relates to 3.10 activity_main.xml price input

## Monetization

### Premium Ad-Free Version and Pro Tier: Offer an in-app purchase to remove non-intrusive banner ads, and provide a paid Pr -- 2/2 reviewers

- **grok-4-0709:** Premium ad-free version unlock: Offer an in-app purchase to remove non-intrusive banner ads (displayed below the total), providing a revenue stream while giving power users an uninterrupted experience, differentiating from free basic calculators.
  - *Context:* General, as a new area for app sustainability
- **gpt-5.2:** Pro tier for exports and presets: Keep the core calculator free, and offer a paid upgrade unlocking advanced features like exports, unlimited presets, saved projects, and branded PDF invoices. This aligns monetization with “power-user” value while preserving the simple baseline experience.
  - *Context:* General (business model extension)

### Subscription for Project Saving: Introduce a monthly subscription to save and load multiple "projects" (grouped calculat

- **grok-4-0709:** Subscription for project saving: Introduce a monthly subscription to save and load multiple "projects" (grouped calculations with names like "Deck Build"), allowing users to resume work across sessions, creating recurring revenue from professional users.
  - *Context:* Relates to 3.11 MainActivity.kt state, extending no-persistence

## Onboarding

### First-Launch Interactive Tutorial and Guidance: On first app open, overlay tooltips guiding through entering price, dime -- 2/2 reviewers

- **grok-4-0709:** First-launch interactive tutorial: On first app open, overlay tooltips guiding through entering price, dimensions, and calculating, with a "Got it" button to dismiss, helping new users (e.g., hobbyists) quickly understand the workflow and reduce initial friction.
  - *Context:* Relates to 3.11 MainActivity.kt onCreate
- **gpt-5.2:** Add first-run sample and guidance: On first launch, prefill example values (or show a short “How to measure” tip) and explain what board feet means with a one-screen primer. This helps novices adopt the app without searching externally, increasing retention.
  - *Context:* General (new first-run UX around the existing single screen)

## Performance Ux

### Real-Time Calculation Preview: As users type dimensions, show a live preview of board feet and cost below the inputs (de -- 2/2 reviewers

- **grok-4-0709:** Real-time calculation preview: As users type dimensions, show a live preview of board feet and cost below the inputs (debounced for smoothness), eliminating the need for the calculate button in simple cases and offering instant feedback for iterative adjustments.
  - *Context:* Relates to 3.11 MainActivity.kt calculate() function
- **gpt-5.2:** Calculate-as-you-type optional mode: Add a toggle that continuously shows computed board feet and cost as fields are filled (with graceful handling of partial inputs). For many users, immediate feedback reduces taps and makes the app feel more like a “calculator” than a “form + submit.”
  - *Context:* Calculate button flow vs real-time updates

## Platform

### Companion Web Version Integration: Design for future web export of calculation logic via Kotlin Multiplatform, allowing 

- **grok-4-0709:** Companion web version integration: Design for future web export of the calculation logic via Kotlin Multiplatform, allowing users to access the same calculator on desktop browsers with sync via QR code scan, extending usability for office-based planning.
  - *Context:* General, building on the Android-focused structure in section 2

### Home Screen Widget for Quick Entry: Provide a lightweight widget where users can enter L/W/T and see board feet instantl

- **gpt-5.2:** Add home screen widget for quick entry: Provide a lightweight widget where users can enter L/W/T and see board feet instantly, optionally with price. Woodworkers may want quick access without opening the full app, especially in a shop setting with gloves or limited time.
  - *Context:* General (Android platform enhancement)

## Security Privacy

### Optional Local Encryption for Totals: If users enable a "save session" feature, encrypt the totalBoardFeet and totalCost -- 2/2 reviewers

- **grok-4-0709:** Optional local encryption for totals: If users enable a "save session" feature, encrypt the totalBoardFeet and totalCost using Android Keystore before storing in SharedPreferences, protecting sensitive cost data for professionals without cloud risks.
  - *Context:* Relates to 3.11 MainActivity.kt state, extending the no-persistence decision
- **gpt-5.2:** Privacy-first stance with optional local backups: Since the spec emphasizes no persistence, consider an explicit privacy statement (“No network access; data stays on device”) and an optional user-initiated local backup/export file for saved presets/projects. This builds trust while keeping users in control.
  - *Context:* Manifest decisions + any future saved data

## Social

### Share Calculation Snapshot: Add share functionality that generates a formatted text or image snapshot of results and tot -- 2/2 reviewers

- **grok-4-0709:** Share calculation snapshot: Add a share button that generates a formatted text or image snapshot of the current result and total (e.g., "Board Foot Calc: 12x6x1 = 0.5 bf @ $5 = $2.50"), shareable via social apps like WhatsApp or Twitter, fostering community discussions among woodworkers.
  - *Context:* Relates to 3.11 MainActivity.kt result and total displays
- **gpt-5.2:** Add copy/share of result and totals: Provide “Copy” and “Share” actions that export the last result and/or the running total in a clean text format (dimensions, bf, cost, price, adjustments). Users often need to message totals to a customer or keep notes for later.
  - *Context:* Result text + Total text area

### Community "Common Dimensions" Templates: Offer optional templates like "2x4 nominal vs actual," "deck boards," "turning 

- **gpt-5.2:** Community “common dimensions” templates: Offer optional templates like “2x4 nominal vs actual,” “deck boards,” “turning blanks,” etc., that populate fields with typical actual dimensions. This reduces measurement confusion and can become a differentiator via curated domain knowledge.
  - *Context:* General (new helper content + presets around dimension entry)

## Ux

### Auto-Focus Next Field on Input: Automatically focus the next input field (e.g., from length to width to thickness) after

- **grok-4-0709:** Auto-focus next field on input: After entering a dimension (e.g., length), automatically focus the next field (width, then thickness) using keyboard "next" actions or on-text-change listeners, streamlining the rapid entry workflow for users calculating multiple boards quickly and reducing taps.
  - *Context:* Relates to 3.10 activity_main.xml and 3.11 MainActivity.kt dimension inputs

### Dark Mode Theme Toggle: Add a settings toggle for dark mode using Material3 dynamic theming, automatically adapting colo

- **grok-4-0709:** Dark mode theme toggle: Add a settings toggle for dark mode using Material3 dynamic theming, automatically adapting colors.xml for low-light environments, improving eye comfort for users working in workshops or evenings.
  - *Context:* Relates to 3.9 themes.xml and 3.8 colors.xml

### Undo Last Calculation Button: Add an "Undo" button that subtracts the most recent board feet and cost from totals and re

- **grok-4-0709:** Undo last calculation button: Add an "Undo" button that subtracts the most recent board feet and cost from totals and restores dimension fields, helping users correct mistakes in multi-entry sessions without full clear.
  - *Context:* Relates to 3.11 MainActivity.kt calculate() and clearAll()

### Inline Validation and Field-Level Errors: Instead of only toasts, show inline errors on specific TextInputLayouts (e.g.,

- **gpt-5.2:** Inline validation and field-level errors: Instead of only toasts, show inline errors on the specific TextInputLayouts (e.g., “Required”, “Must be > 0”) and clear them when corrected. This improves clarity, reduces repetitive toast dismissal, and makes the “what went wrong” visually obvious.
  - *Context:* Validation flow in MainActivity.calculate() + TextInputLayout usage

### Quick "Repeat Last Dimensions" Action: Provide a button/icon to reuse the last entered dimensions (and quantity) without

- **gpt-5.2:** Quick “Repeat last dimensions” action: Provide a button/icon to reuse the last entered dimensions (and quantity) without retyping, useful when measuring similar boards that only differ slightly or when you accidentally cleared too early. This supports fast repetitive workflows.
  - *Context:* After-calculate behavior (dimensions auto-clear)

## Other

### Multi-Language Localization: Expand strings.xml with translations for key languages (e.g., Spanish, French) and adapt fo

- **grok-4-0709:** Multi-language localization: Expand strings.xml with translations for key languages (e.g., Spanish, French) and adapt formats for regional number separators, broadening appeal to international users in the woodworking community.
  - *Context:* Relates to 3.7 strings.xml

## High-Consensus Ideas

The following suggestions were independently raised by multiple reviewers, indicating strong signal:

- **Unit Conversion and Selection: Add unit selectors with common presets for dimensions (inches, feet+inches, mm) and price** (2/2 reviewers: grok-4-0709, gpt-5.2)
- **Export Data to Spreadsheet/CSV: Include a button to export running totals and individual calculations as a CSV file shar** (2/2 reviewers: grok-4-0709, gpt-5.2)
- **Screen Reader and Keyboard Navigation Optimization: Enhance accessibility with content descriptions and hints that inclu** (2/2 reviewers: grok-4-0709, gpt-5.2)
- **Premium Ad-Free Version and Pro Tier: Offer an in-app purchase to remove non-intrusive banner ads, and provide a paid Pr** (2/2 reviewers: grok-4-0709, gpt-5.2)
- **Fractional Input Parsing and Quick-Pick Chips: Extend dimension parsing to handle fractional inputs like "1 1/2" or "1.5** (2/2 reviewers: grok-4-0709, gpt-5.2)
- **Share Calculation Snapshot: Add share functionality that generates a formatted text or image snapshot of results and tot** (2/2 reviewers: grok-4-0709, gpt-5.2)
- **Optional Local Encryption for Totals: If users enable a "save session" feature, encrypt the totalBoardFeet and totalCost** (2/2 reviewers: grok-4-0709, gpt-5.2)
- **First-Launch Interactive Tutorial and Guidance: On first app open, overlay tooltips guiding through entering price, dime** (2/2 reviewers: grok-4-0709, gpt-5.2)
- **Real-Time Calculation Preview: As users type dimensions, show a live preview of board feet and cost below the inputs (de** (2/2 reviewers: grok-4-0709, gpt-5.2)
- **Waste Factor Adjustment and Other Toggles: Include a slider or toggles to apply waste percentage (e.g., +10% for cuts), ** (2/2 reviewers: grok-4-0709, gpt-5.2)

## Compiled Suggestion Report

# Specification Enrichment Report

## Overview
This report synthesizes 27 distinct suggestions from domain experts across 13 thematic areas. **10 high-consensus ideas** were identified where multiple reviewers independently proposed similar enhancements, representing strong signals for prioritization. The suggestions focus heavily on accessibility, data input flexibility, professional workflow support, and user experience refinement.

---

## Accessibility

### Screen Reader and Keyboard Navigation Optimization -- 2/2 reviewers
Enhance accessibility with content descriptions and hints that include unit expectations (e.g., "Enter length in inches"), set explicit IME actions (Next/Done), move focus predictably across fields, ensure TalkBack labels read well, and consider larger tap targets and high-contrast themes for shop environments. This improves inclusivity for visually impaired users and accommodates challenging work environments without altering the core layout.

### Voice Command Input Support -- 1/2 reviewers
Integrate Android's speech-to-text for dictating dimensions and price (e.g., "length twelve width six thickness one"), making the app hands-free for users in workshops where typing is impractical. This extends accessibility benefits to users who need to operate the app while handling materials.

---

## Content

### Educational Board Foot Explainer -- 1/2 reviewers
Add an info icon linking to a dialog explaining the board foot formula and examples (e.g., "A board foot is 144 cubic inches of wood"), enriching the app with value for beginners and positioning it as an educational tool. This helps novices understand the domain vocabulary and increases retention by reducing external research needs.

---

## Data Model

### Fractional Input Parsing and Quick-Pick Chips -- 2/2 reviewers
Extend dimension parsing to handle fractional inputs like "1 1/2" or "1.5" interchangeably, storing them as decimals in the model. Additionally offer optional chips/buttons for common fractions (1/8, 1/4, 3/8, 1/2, 5/8, 3/4) that append to the current dimension field. This reduces typing friction for users accustomed to carpentry notations and matches workshop reality where thickness is frequently a standard fraction.

### Line-Item List with Undo/Remove -- 1/2 reviewers
Introduce a simple list of calculated line items (each with dimensions, quantity, bf, cost) beneath the result, with swipe-to-delete and a one-tap Undo snackbar. Running totals become auditable and correctable when entering many boards quickly, providing transparency for professional workflows.

### Save Named Price Presets Per Species/Supplier -- 1/2 reviewers
Let users save multiple named price presets (e.g., "Walnut S4S $12.50/bf", "Pine rough $3.25/bf") and switch quickly. Real pricing varies by wood species, grade, and supplier; presets reduce repetitive entry and errors for professionals working with diverse materials.

---

## Features

### Unit Conversion and Selection -- 2/2 reviewers
Add unit selectors with common presets for dimensions (inches, feet+inches, mm) and price basis (per board foot, per cubic foot/meter, per piece). Also support feet-and-inches entry mode (e.g., 8' 6 1/2") and fractional inputs to reduce mental math and accommodate different regional measurement preferences, enhancing global usability without changing the core workflow.

### Waste Factor Adjustment and Other Toggles -- 2/2 reviewers
Include a slider or toggles to apply waste percentage (e.g., +10% for cuts), sales tax %, and discount % (e.g., contractor pricing), automatically adjusting totals to help users plan for real-world material losses and provide more accurate project estimates. This makes totals "real-world accurate" while keeping defaults off for simplicity.

### Preset Lumber Size Buttons -- 1/2 reviewers
Include quick-select buttons for common lumber sizes (e.g., 2x4, 1x6) that auto-fill dimensions, speeding up repetitive calculations for standard boards and adding convenience for frequent users like builders.

### Quantity Multiplier Per Line Item -- 1/2 reviewers
Add a Quantity field (default 1) so users can price multiple identical boards in one calculation. This is a highly common workflow (e.g., "8 boards at 96 x 6 x 1") and complements the running total behavior, reducing calculation steps.

---

## Integrations

### Export Data to Spreadsheet/CSV -- 2/2 reviewers
Include a button to export running totals and individual calculations as a CSV file shareable via Android's share sheet (e.g., to Google Sheets or email). Optionally generate PDF "cut lists" with line items, dates, and supplier info for contractor workflows, enabling seamless integration with invoicing or inventory tools.

### Live Lumber Price API Fetch -- 1/2 reviewers
Integrate with a public API (e.g., for current wood prices) to auto-populate or suggest the price field based on wood type selection, providing real-time market insights and saving users research time.

---

## Monetization

### Premium Ad-Free Version and Pro Tier -- 2/2 reviewers
Offer an in-app purchase to remove non-intrusive banner ads, and provide a paid Pro tier unlocking advanced features like exports, unlimited presets, saved projects, and branded PDF invoices. This differentiates power users from the free basic calculator while creating revenue streams aligned with "power-user" value.

### Subscription for Project Saving -- 1/2 reviewers
Introduce a monthly subscription to save and load multiple "projects" (grouped calculations with names like "Deck Build"), allowing users to resume work across sessions and creating recurring revenue from professional users.

---

## Onboarding

### First-Launch Interactive Tutorial and Guidance -- 2/2 reviewers
On first app open, overlay tooltips guiding through entering price, dimensions, and calculating with a "Got it" button to dismiss. Additionally, prefill example values or show a short "How to measure" tip and explain what board feet means with a one-screen primer to help novices adopt the app without external searches.

---

## Other

### Multi-Language Localization -- 1/2 reviewers
Expand strings.xml with translations for key languages (e.g., Spanish, French) and adapt formats for regional number separators, broadening appeal to international users in the woodworking community.

---

## Performance Ux

### Real-Time Calculation Preview -- 2/2 reviewers
As users type dimensions, show a live preview of board feet and cost below the inputs (debounced for smoothness), eliminating the need for the calculate button in simple cases and offering instant feedback for iterative adjustments. Include an optional toggle for continuous calculation to make the app feel more like a "calculator" than a "form + submit."

---

## Platform

### Companion Web Version Integration -- 1/2 reviewers
Design for future web export of calculation logic via Kotlin Multiplatform, allowing users to access the same calculator on desktop browsers with sync via QR code scan, extending usability for office-based planning.

### Home Screen Widget for Quick Entry -- 1/2 reviewers
Provide a lightweight widget where users can enter L/W/T and see board feet instantly, optionally with price. Woodworkers may want quick access without opening the full app, especially in a shop setting with gloves or limited time.

---

## Security Privacy

### Optional Local Encryption for Totals -- 2/2 reviewers
If users enable a "save session" feature, encrypt the totalBoardFeet and totalCost using Android Keystore before storing in SharedPreferences, protecting sensitive cost data for professionals without cloud risks. Also include an explicit privacy statement ("No network access; data stays on device") and optional user-initiated local backup/export file to build trust.

---

## Social

### Share Calculation Snapshot -- 2/2 reviewers
Add share functionality that generates a formatted text or image snapshot of results and totals (e.g., "Board Foot Calc: 12x6x1 = 0.5 bf @ $5 = $2.50"), shareable via social apps like WhatsApp, Twitter, or messaging. Also provide copy functionality for clean text export, enabling users to message totals to customers or keep notes.

### Community "Common Dimensions" Templates -- 1/2 reviewers
Offer optional templates like "2x4 nominal vs actual," "deck boards," "turning blanks," etc., that populate fields with typical actual dimensions. This reduces measurement confusion and can become a differentiator via curated domain knowledge.

---

## Ux

### Auto-Focus Next Field on Input -- 1/2 reviewers
Automatically focus the next input field (e.g., from length to width to thickness) after entry using keyboard "next" actions or on-text-change listeners, streamlining rapid entry workflow for users calculating multiple boards quickly and reducing taps.

### Dark Mode Theme Toggle -- 1/2 reviewers
Add a settings toggle for dark mode using Material3 dynamic theming, automatically adapting colors.xml for low-light environments, improving eye comfort for users working in workshops or evenings.

### Undo Last Calculation Button -- 1/2 reviewers
Add an "Undo" button that subtracts the most recent board feet and cost from totals and restores dimension fields, helping users correct mistakes in multi-entry sessions without full clear.

### Inline Validation and Field-Level Errors -- 1/2 reviewers
Instead of only toasts, show inline errors on specific TextInputLayouts (e.g., "Required", "Must be > 0") and clear them when corrected, improving clarity and making "what went wrong" visually obvious.

### Quick "Repeat Last Dimensions" Action -- 1/2 reviewers
Provide a button/icon to reuse the last entered dimensions (and quantity) without retyping, useful when measuring similar boards that only differ slightly or when you accidentally cleared too early, supporting fast repetitive workflows.

---

## High-Consensus Ideas

The following 10 suggestions were independently proposed by **both reviewers**, indicating strong alignment on priority enhancements:

1. **Screen Reader and Keyboard Navigation Optimization** — Accessibility improvements for inclusive design
2. **Fractional Input Parsing and Quick-Pick Chips** — Natural carpentry notation support
3. **Unit Conversion and Selection** — Multi-unit and regional support
4. **Waste Factor Adjustment and Other Toggles** — Real-world project accuracy
5. **Export Data to Spreadsheet/CSV** — Professional workflow integration
6. **Premium Ad-Free Version and Pro Tier** — Sustainable monetization model
7. **First-Launch Interactive Tutorial and Guidance** — Reduced adoption friction
8. **Real-Time Calculation Preview** — Instant feedback and reduced taps
9. **Optional Local Encryption for Totals** — Professional data security
10. **Share Calculation Snapshot** — Social and professional sharing

These represent the strongest signals for immediate implementation consideration.

## Cost Breakdown

| Model | Cost (USD) |
|---|---|
| gpt-5.2 | $0.0375 |
| grok-4-0709 | $0.0342 |
| minimax-m2.5 | $0.0057 |
| kimi-k2-thinking | $0.0135 |
| **Total** | **$0.0910** |
