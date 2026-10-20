import { useEffect, useRef, useState, type ChangeEvent } from 'react';
import { Badge, Button, Card, ErrorNote, IconButton, Spinner, cx, inputClass } from '../../components/ui';
import { copyText } from '../../lib/dom';
import { formatBytes } from '../../lib/format';
import { errorText, useHub } from '../../lib/hub';
import type { TranscriptionResult } from '../../lib/types';

interface Clip {
  blob: Blob;
  name: string;
}

function extensionFor(mime: string): string {
  if (mime.includes('ogg')) return 'ogg';
  if (mime.includes('mp4') || mime.includes('aac')) return 'm4a';
  if (mime.includes('wav')) return 'wav';
  if (mime.includes('mpeg')) return 'mp3';
  return 'webm';
}

function canRecord(): boolean {
  return typeof MediaRecorder !== 'undefined' && typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia;
}

/** Speech to text through `POST /llm/transcribe`; the text can be copied or dropped into the chat input. */
export default function TranscribePanel({ onSend, onClose }: { onSend: (text: string) => void; onClose: () => void }) {
  const { client, notify } = useHub();
  const [clip, setClip] = useState<Clip | null>(null);
  const [language, setLanguage] = useState('');
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TranscriptionResult | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!recording) return undefined;
    const started = Date.now();
    const t = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 500);
    return () => window.clearInterval(t);
  }, [recording]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      if (recorderRef.current && recorderRef.current.state !== 'inactive') recorderRef.current.stop();
      streamRef.current?.getTracks().forEach((t) => t.stop());
    },
    [],
  );

  const onFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setClip({ blob: file, name: file.name });
    setResult(null);
    setError(null);
  };

  const startRecording = async () => {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const recorder = new MediaRecorder(stream);
      const chunks: Blob[] = [];
      recorder.ondataavailable = (ev) => {
        if (ev.data.size > 0) chunks.push(ev.data);
      };
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
        const type = recorder.mimeType || chunks[0]?.type || 'audio/webm';
        const blob = new Blob(chunks, { type });
        if (blob.size > 0) {
          setClip({ blob, name: `recording.${extensionFor(type)}` });
          setResult(null);
        }
        setRecording(false);
      };
      recorderRef.current = recorder;
      recorder.start();
      setElapsed(0);
      setRecording(true);
    } catch (err) {
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      setError(`Microphone unavailable: ${errorText(err)}`);
    }
  };

  const stopRecording = () => {
    recorderRef.current?.stop();
    recorderRef.current = null;
  };

  const transcribe = async () => {
    if (!clip || busy) return;
    setBusy(true);
    setError(null);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      setResult(await client.transcribe(clip.blob, { filename: clip.name, language: language.trim() || undefined }, ctrl.signal));
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) setError(errorText(err));
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  };

  const copy = async () => {
    if (!result?.text) return;
    const ok = await copyText(result.text);
    notify(ok ? 'Copied to clipboard' : 'Copy failed: select the text and copy it manually', ok ? 'ok' : 'error');
  };

  return (
    <Card className="space-y-3 p-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-[11px] font-medium uppercase tracking-[0.14em] text-ink-400">Transcribe</h3>
        <IconButton icon="x" label="Close transcribe" onClick={onClose} className="h-8 w-8" />
      </div>

      <div className="flex flex-wrap gap-2">
        <Button icon="upload" onClick={() => fileRef.current?.click()} disabled={recording || busy}>
          Audio file
        </Button>
        <input ref={fileRef} type="file" accept="audio/*,video/webm,video/mp4" className="hidden" onChange={onFile} aria-label="Audio file" />
        {canRecord() &&
          (recording ? (
            <Button variant="danger" icon="stop" onClick={stopRecording}>
              Stop {elapsed}s
            </Button>
          ) : (
            <Button icon="mic" onClick={() => void startRecording()} disabled={busy}>
              Record
            </Button>
          ))}
        <input
          aria-label="Language"
          className={cx(inputClass, 'w-24 flex-none font-mono')}
          value={language}
          onChange={(e) => setLanguage(e.target.value)}
          placeholder="lang"
          maxLength={8}
          spellCheck={false}
        />
      </div>

      {recording && (
        <p className="flex items-center gap-2 text-xs text-danger-400">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-danger-400" />
          Recording...
        </p>
      )}

      {clip && !recording && (
        <div className="flex items-center gap-2">
          <p className="min-w-0 flex-1 truncate text-xs text-ink-300">
            {clip.name} <span className="font-mono text-ink-500">{formatBytes(clip.blob.size)}</span>
          </p>
          <Button variant="primary" icon="send" onClick={() => void transcribe()} disabled={busy}>
            {busy ? <Spinner /> : 'Transcribe'}
          </Button>
        </div>
      )}

      {error && <ErrorNote message={error} />}

      {result && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-1.5">
            {result.model && <Badge>{result.model}</Badge>}
            {result.language && <Badge>{result.language}</Badge>}
            {typeof result.duration === 'number' && <Badge>{result.duration.toFixed(1)} s</Badge>}
          </div>
          <p className="max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-ink-700/70 bg-ink-950 px-3 py-2 text-sm text-ink-100 select-text">
            {result.text || <span className="text-ink-500">(no speech detected)</span>}
          </p>
          <div className="flex justify-end gap-2">
            <Button icon="copy" onClick={() => void copy()} disabled={!result.text}>
              Copy
            </Button>
            <Button variant="primary" icon="chat" onClick={() => onSend(result.text)} disabled={!result.text}>
              Send to chat
            </Button>
          </div>
        </div>
      )}

      {!clip && !recording && !error && (
        <p className="text-xs text-ink-500">Pick an audio file{canRecord() ? ' or record a voice note' : ''}; the text can go straight into the chat box.</p>
      )}
    </Card>
  );
}
