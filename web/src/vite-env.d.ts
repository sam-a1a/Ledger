/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API origin. Empty means same-origin (production, behind nginx). */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
