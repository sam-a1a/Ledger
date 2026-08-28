import { useEffect, useState } from "react";
import * as api from "../api/account";
import type { Catalog } from "../api/account";

/** Whether a description was written, generated, or invented from the profile. */
const PROVENANCE: Record<string, { label: string; title: string }> = {
  seed: { label: "human", title: "Written by hand and reviewed." },
  llm: { label: "generated", title: "Written by a model from the column profile." },
  derived: {
    label: "derived",
    title: "Assembled from the profile because nothing better exists. A gap worth filling.",
  },
};

/**
 * What this role can ask about.
 *
 * Served by `/api/catalog` rather than the `list_columns` tool: asking through
 * the model spends a turn, and costs money, to answer a question the server
 * already knows.
 *
 * It is also the access boundary made visible. The list is shorter for a
 * viewer than for an analyst, so the restriction is something you can see
 * rather than something you find out by being refused.
 */
export function CatalogDrawer({
  token,
  open,
  onClose,
}: {
  token: string;
  open: boolean;
  onClose: () => void;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    if (!open || catalog) return;
    let live = true;
    api
      .catalog(token)
      .then((found) => live && setCatalog(found))
      .catch(() => live && setError("The catalogue could not be loaded."));
    return () => {
      live = false;
    };
  }, [open, token, catalog]);

  useEffect(() => {
    if (!open) return;
    const escape = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [open, onClose]);

  if (!open) return null;

  const needle = filter.trim().toLowerCase();
  const columns = (catalog?.columns ?? []).filter(
    (column) =>
      !needle ||
      column.name.includes(needle) ||
      (column.description ?? "").toLowerCase().includes(needle),
  );

  return (
    <aside className="catalog-drawer" data-testid="catalog-drawer" aria-label="Catalogue">
      <header>
        <div>
          <h2>Columns</h2>
          {catalog && (
            <p className="catalog-meta" data-testid="catalog-meta">
              {catalog.columns.length} available to a {catalog.role} ·{" "}
              {catalog.total_rows.toLocaleString()} rows
            </p>
          )}
        </div>
        <button type="button" onClick={onClose} data-testid="catalog-close" aria-label="Close">
          ×
        </button>
      </header>

      <input
        type="search"
        placeholder="Filter"
        value={filter}
        data-testid="catalog-filter"
        onChange={(event) => setFilter(event.target.value)}
      />

      {error && <p className="error-note">{error}</p>}

      <ul>
        {columns.map((column) => {
          const provenance = column.description_source
            ? PROVENANCE[column.description_source]
            : undefined;
          return (
            <li key={column.name} data-testid="catalog-column" data-column={column.name}>
              <div className="catalog-head">
                <code>{column.name}</code>
                <span className="catalog-type">{column.semantic_type}</span>
              </div>
              {column.description && <p>{column.description}</p>}
              <div className="catalog-facts">
                {provenance && (
                  <span
                    className={`provenance provenance-${column.description_source}`}
                    title={provenance.title}
                    data-testid="catalog-provenance"
                  >
                    {provenance.label}
                  </span>
                )}
                {column.unit && <span>{column.unit}</span>}
                <span title="Fraction of rows where this column is null">
                  {(column.null_fraction * 100).toFixed(1)}% null
                </span>
                <span title="Approximate distinct values">
                  {column.approx_distinct.toLocaleString()} distinct
                </span>
              </div>
              {column.caveat && <p className="catalog-caveat">{column.caveat}</p>}
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
