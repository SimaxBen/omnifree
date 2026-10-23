// Picks the previewer for a file. Heavy ones (pdf.js, mammoth, SheetJS, JSZip) are separate
// chunks loaded on first use, so the main bundle stays small.

import { Component, Suspense, lazy, type ErrorInfo, type ReactNode } from 'react';
import { extensionOf, previewerOf } from '../../lib/files';
import type { PreviewSource } from '../../lib/preview/source';
import { Loading, PreviewError, PreviewMessage } from './common';
import { MediaPreview, ZoomImage } from './MediaPreviews';
import { CodePreview, MarkdownPreview } from './TextPreviews';

const PdfViewer = lazy(() => import('./PdfViewer'));
const DocPreview = lazy(() => import('./DocPreview'));
const SheetPreview = lazy(() => import('./SheetPreview'));
const SlidesPreview = lazy(() => import('./SlidesPreview'));
const ZipPreview = lazy(() => import('./ZipPreview'));

/** A crash inside one previewer shows its error with the download fallback, not a blank viewer. */
class PreviewBoundary extends Component<{ src: PreviewSource; children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error('Preview crashed', error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      const chunk = /dynamically imported module|Failed to fetch|Importing a module script/i.test(this.state.error.message);
      return <PreviewError src={this.props.src} error={chunk ? 'The previewer could not be loaded (offline?). Try again when connected.' : this.state.error} />;
    }
    return this.props.children;
  }
}

export interface PreviewProps {
  src: PreviewSource;
  /** Swipe to the previous/next file (images). */
  onSwipe?: (dir: -1 | 1) => void;
  /** Closes the whole viewer (e.g. before opening the note made from a transcript). */
  onClose?: () => void;
  /** Archive nesting depth (zip inside zip). */
  depth?: number;
}

export function PreviewBody({ src, onSwipe, onClose, depth = 0 }: PreviewProps) {
  const which = previewerOf(src.path, src.contentType);
  const ext = extensionOf(src.path);
  let body: ReactNode;
  switch (which) {
    case 'image':
      body = <ZoomImage src={src} onSwipe={onSwipe} />;
      break;
    case 'video':
    case 'audio':
      body = <MediaPreview src={src} kind={which} onClose={onClose} />;
      break;
    case 'pdf':
      body = <PdfViewer src={src} />;
      break;
    case 'docx':
    case 'odt':
      body = <DocPreview src={src} format={which} />;
      break;
    case 'sheet':
    case 'csv':
      body = <SheetPreview src={src} />;
      break;
    case 'pptx':
      body = <SlidesPreview src={src} />;
      break;
    case 'zip':
      body =
        depth >= 3 ? (
          <PreviewMessage src={src} icon="folder" title="Archive nested too deep">
            Download it to open it.
          </PreviewMessage>
        ) : (
          <ZipPreview src={src} depth={depth} />
        );
      break;
    case 'markdown':
      body = <MarkdownPreview src={src} />;
      break;
    case 'code':
      body = <CodePreview src={src} />;
      break;
    case 'text':
      body = <CodePreview src={src} plain />;
      break;
    case 'legacy':
      body = (
        <PreviewMessage src={src} icon={ext === 'ppt' ? 'chart' : 'book'} title={`No preview for old .${ext} files`}>
          This is the pre-2007 {ext === 'ppt' ? 'PowerPoint' : 'Word'} format, which the app can't read. Open it in an office app, or save it as .{ext}x to preview it here.
        </PreviewMessage>
      );
      break;
    case 'archive':
      body = (
        <PreviewMessage src={src} icon="folder" title={`No preview for .${ext || 'archive'} files`}>
          Only .zip archives can be browsed in the app. Download it to extract it.
        </PreviewMessage>
      );
      break;
    default:
      body = (
        <PreviewMessage src={src} icon="file" title="No preview for this file type">
          {ext ? `.${ext} files open in another app.` : undefined}
        </PreviewMessage>
      );
  }
  return (
    <PreviewBoundary key={src.id} src={src}>
      <Suspense fallback={<Loading label="Loading the previewer" />}>{body}</Suspense>
    </PreviewBoundary>
  );
}
