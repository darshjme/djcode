# Original UI design-context packs

DJcode bundles seven reusable design references. They contain original prose and SVG wireframes, not finished application components. No account, network request or Mobbin subscription is needed to read them. The packs do not provide access to Mobbin’s library or redistribute its screenshots.

project by Darshan Kumar Joshi

| ID | Use it for |
|---|---|
| `dashboard` | Decision order, metric scope, freshness and partial failures |
| `settings` | Scoped forms, unsaved drafts, conflict handling and separate destructive actions |
| `command-palette` | Search, active selection, keyboard operation and permission-aware execution |
| `onboarding-auth` | Reversible connection steps, explicit capabilities and verification |
| `data-table` | Sorting, filtering, selection scope, pagination and row actions |
| `usage-billing` | Units, periods, estimates, plan review and retry-safe mutations |
| `empty-error` | Distinct empty, filtered, offline, permission and failure states |

Each pack includes a concrete fictional example; layout and responsiveness; states; accessibility and keyboard guidance; implementation considerations; verification scenarios; sources and license. The accompanying SVG illustrates one possible composition. It has no functional buttons or authentication logic. Follow the written interaction guidance when implementing it.

## Reading and reuse

```sh
djcode --design-packs
djcode --design-pack data-table
djcode --design-pack data-table "Improve the releases table"
djcode --design-pack data-table --design-export ./new-table-reference
```

Export requires a new destination directory and refuses an existing path. It writes the selected Markdown/SVG pair so the relative image link remains usable. Listing and reading packs requires no model inference. With a task prompt, the selected guidance becomes reference context for that task; normal provider and tool permissions still apply.

In the interactive TUI or classic REPL, `/design` lists packs, `/design data-table` selects session guidance, and `/design off` clears it. Selection replaces the prior design block instead of accumulating it on every turn.

The package APIs work offline from an installed wheel, including resources loaded through `importlib.resources`:

```python
from djcode.design_packs import list_packs, get_pack, get_example

for pack in list_packs():
    print(pack["id"], pack["summary"])

markdown = get_pack("data-table")
svg = get_example("data-table")
```

Unknown identifiers raise `ValueError`; identifiers are an allowlist, not filesystem paths. Metadata dictionaries returned by `list_packs()` can be modified by the caller without changing the bundled registry. Markdown references the matching SVG with a relative filename, so keep exported pairs together.

Use a pack as context for a concrete task: name the users, data contract, current framework and the state that needs improvement. Ask the coding agent to inspect existing components first, implement the relevant behavior, then test the listed failure and keyboard scenarios. The pack should not override project requirements or authorize external actions. Guidance alone does not establish WCAG conformance or guarantee usable generated code.

## Provenance and license

The prose, fictional examples and SVG compositions were created for DJcode and are distributed under the repository’s MIT license. There are no bundled third-party screenshots, fonts, logos, paid assets, credentials or copied reference implementations. Retain the project license when redistributing this material. Linked sources retain their own terms; the DJcode license does not relicense them.

Public sources reviewed on 2026-09-09:

- [W3C ARIA Authoring Practices patterns](https://www.w3.org/WAI/ARIA/apg/patterns/) for interaction models, with individual references in each pack.
- [WCAG 2.2](https://www.w3.org/TR/WCAG22/) and the linked Understanding documents for accessibility considerations. Understanding documents and APG provide guidance; they are not a substitute for evaluating the complete implementation.
- [W3C document license](https://www.w3.org/copyright/document-license-2023/) for the reference material’s separate license boundary. DJcode links to these documents and supplies original examples rather than copying their assets or source code.

No Mobbin access was used or needed for these packs. They make no claim of endorsement, affiliation or comparative design quality.
