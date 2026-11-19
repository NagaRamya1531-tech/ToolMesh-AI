# Engineering contribution guide

Start from the [README setup and test commands](../README.md#development-and-testing). The [specification](ToolMesh_AI_SRS.md) defines milestone scope. The [changelog](../CHANGELOG.md) records completed work and its checks.

## Milestone definition of done

- Update the README capability summary and roadmap. Use **Implemented** for working and verified capabilities, **In progress** for started work awaiting acceptance, and **Planned** for work without an implementation.
- Update the affected architecture diagrams, data model, runbook, and threat model. Write or supersede an ADR when a significant engineering decision changes.
- Record actual commands, test counts, reduced coverage, and open failures in the changelog. Verify installation and configuration instructions on a fresh setup when bootstrap changes.
- Use screenshots from the running application. Label sandbox and synthetic data. Keep credentials and personal data out of images.
- For performance claims, include dataset and parameters, environment and versions, measurement scope, actual results, and a link to raw evidence. Publish misses and limitations alongside successes.
- Verify repository-relative links and heading anchors. Render changed Mermaid diagrams locally and check their GitHub Markdown preview when available.
- Commit and push the verified milestone, then pause for review.

## Diagram conventions

Use native GitHub Mermaid fences. Architecture flowcharts use indigo clients, cyan gateway planes, green state and amber integrations. Use identical labels in the standalone SVG and its Mermaid source. Put requests left to right in flowcharts. Use rectangles for actors, applications, and operations, cylinders for datastores, and diamonds only for decisions. Name trust boundaries in plain language and preserve the same component names across views.

Use active directional labels and regular Mermaid syntax for flows, sequence diagrams, and ERDs. Keep diagrams readable without color. Avoid external images, custom HTML, renderer directives, and layout extensions. Preview diagrams on GitHub using its native Markdown renderer. [GitHub diagram documentation](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams)
