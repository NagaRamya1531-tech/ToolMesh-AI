# ToolMesh AI repository guidance

## Permanent product vision

- [ToolMesh AI Product Vision](docs/PRODUCT_VISION.md) is the authoritative, permanent product definition. Codex must read and follow it before making architectural or feature changes.
- Preserve the original M0–M8 functionality. Obtain explicit approval from the project owner before changing the product identity, business problem, five product pillars, or five expansion areas.
- Ensure every new feature aligns with the locked product vision and serves at least one of its five pillars.
- In documentation and status reports, accurately distinguish implemented, incomplete, and planned functionality.

## Milestone and documentation guidance

- Use [the product specification](docs/ToolMesh_AI_SRS.md) and the user's milestone authorization to scope implementation.
- Treat [the documentation checklist](docs/contributing.md#milestone-definition-of-done) as part of every milestone. Update the README, affected engineering documentation, roadmap, and changelog before reporting completion.
- Keep Mermaid diagrams compatible with GitHub. Use the conventions in [the contribution guide](docs/contributing.md#diagram-conventions) and verify changed diagrams.
- Label synthetic data and planned functionality. Publish benchmark numbers only from actual runs with reproduction details and raw evidence.
- After verification and documentation, commit and push the authorized milestone to `origin/main` and stop for review before starting another milestone.
