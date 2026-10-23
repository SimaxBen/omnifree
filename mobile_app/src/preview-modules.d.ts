// Typings for preview libraries that ship none.

declare module 'highlight.js/lib/languages/*' {
  import type { LanguageFn } from 'highlight.js';
  const language: LanguageFn;
  export default language;
}

declare module 'mammoth' {
  interface MammothImage {
    contentType: string;
    readAsBase64String(): Promise<string>;
  }
  interface MammothResult {
    value: string;
    messages: { type: string; message: string }[];
  }
  interface MammothOptions {
    convertImage?: unknown;
    styleMap?: string | string[];
    includeDefaultStyleMap?: boolean;
    ignoreEmptyParagraphs?: boolean;
    idPrefix?: string;
  }
  const mammoth: {
    convertToHtml(input: { arrayBuffer: ArrayBuffer }, options?: MammothOptions): Promise<MammothResult>;
    images: {
      imgElement(fn: (image: MammothImage) => Promise<Record<string, string>>): unknown;
    };
  };
  export default mammoth;
}
