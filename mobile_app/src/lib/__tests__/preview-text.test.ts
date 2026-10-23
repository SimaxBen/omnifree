import { describe, expect, it } from 'vitest';
import { codeLanguage, fileKind, isTextual, previewerOf } from '../files';
import { detectDelimiter, looksNumeric, parseDelimited } from '../preview/delimited';
import { splitHighlighted } from '../preview/lines';
import { findInPages } from '../preview/pdfSearch';

describe('delimiter detection', () => {
  it('finds comma, semicolon, tab and pipe', () => {
    expect(detectDelimiter('a,b,c\n1,2,3\n4,5,6\n')).toBe(',');
    expect(detectDelimiter('name;amount\nrent;4500\nfood;1200\n')).toBe(';');
    expect(detectDelimiter('a\tb\tc\n1\t2\t3\n')).toBe('\t');
    expect(detectDelimiter('a|b\n1|2\n3|4\n')).toBe('|');
  });

  it('keeps semicolons when the numbers use decimal commas', () => {
    expect(detectDelimiter('produit;prix;qte\npain;1,50;2\nlait;0,95;1\n')).toBe(';');
  });

  it('ignores separators inside quotes and honours the tsv extension', () => {
    expect(detectDelimiter('"x; y; z",b\n"p; q; r",d\n"1;2;3",f\n')).toBe(',');
    expect(detectDelimiter('a,b\n1,2', 'tsv')).toBe('\t');
    expect(detectDelimiter('single column\nvalue\n')).toBe(',');
  });

  it('parses quotes, doubled quotes, CRLF and newlines in fields, with a row cap', () => {
    const t = parseDelimited('﻿name,note\r\n"Smith, J","said ""hi""\nthen left"\r\nDoe,\r\n', ',');
    expect(t.rows).toEqual([
      ['name', 'note'],
      ['Smith, J', 'said "hi"\nthen left'],
      ['Doe', ''],
    ]);
    expect(t.columns).toBe(2);
    expect(t.truncated).toBe(false);
    const capped = parseDelimited('1\n2\n3\n4\n', ',', 2);
    expect(capped.rows).toEqual([['1'], ['2']]);
    expect(capped.truncated).toBe(true);
    expect(parseDelimited('1\n2\n', ',', 2).truncated).toBe(false);
  });

  it('spots numeric cells', () => {
    for (const v of ['12', '-3.5', '1 234,56', '1,234.56', '45%']) expect(looksNumeric(v)).toBe(true);
    for (const v of ['', 'abc', '12a', '2026-01-01']) expect(looksNumeric(v)).toBe(false);
  });
});

describe('file kinds and previewers', () => {
  it('knows the new kinds and names', () => {
    expect(fileKind('deck.pptx')).toBe('slides');
    expect(fileKind('old.ppt')).toBe('slides');
    expect(fileKind('a/Dockerfile')).toBe('code');
    expect(fileKind('.env')).toBe('code');
    expect(fileKind('Main.kt')).toBe('code');
    expect(fileKind('book.xlsm')).toBe('sheet');
    expect(fileKind('x.tar.xz')).toBe('archive');
    expect(fileKind('report.docx')).toBe('doc');
    expect(isTextual('sheet')).toBe(false);
    expect(isTextual('code')).toBe(true);
  });

  it('routes each file to its previewer', () => {
    const cases: [string, string][] = [
      ['a.pdf', 'pdf'],
      ['a.DOCX', 'docx'],
      ['a.odt', 'odt'],
      ['a.doc', 'legacy'],
      ['a.ppt', 'legacy'],
      ['a.pptx', 'pptx'],
      ['a.odp', 'none'],
      ['a.xlsx', 'sheet'],
      ['a.xls', 'sheet'],
      ['a.ods', 'sheet'],
      ['a.csv', 'csv'],
      ['a.tsv', 'csv'],
      ['a.zip', 'zip'],
      ['a.rar', 'archive'],
      ['a.7z', 'archive'],
      ['a.tar.gz', 'archive'],
      ['README.md', 'markdown'],
      ['a.markdown', 'markdown'],
      ['a.py', 'code'],
      ['a.txt', 'text'],
      ['a.svg', 'image'],
      ['a.heic', 'none'],
      ['a.m4a', 'audio'],
      ['a.mp4', 'video'],
      ['a.bin', 'none'],
    ];
    for (const [name, want] of cases) expect([name, previewerOf(name)]).toEqual([name, want]);
    expect(previewerOf('blob', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')).toBe('docx');
    expect(previewerOf('blob', 'application/zip')).toBe('zip');
  });

  it('maps code files to highlight.js languages', () => {
    expect(codeLanguage('a.ts')).toBe('typescript');
    expect(codeLanguage('a.html')).toBe('xml');
    expect(codeLanguage('a.toml')).toBe('ini');
    expect(codeLanguage('Dockerfile')).toBe('dockerfile');
    expect(codeLanguage('a.rs')).toBe('rust');
    expect(codeLanguage('a.txt')).toBe('');
  });
});

describe('highlighted lines', () => {
  it('closes and reopens spans across line breaks', () => {
    const html = 'a <span class="hljs-comment">/* one\ntwo */</span> b\n<span class="hljs-string">"x"</span>';
    expect(splitHighlighted(html)).toEqual([
      'a <span class="hljs-comment">/* one</span>',
      '<span class="hljs-comment">two */</span> b',
      '<span class="hljs-string">"x"</span>',
    ]);
  });
});

describe('pdf search', () => {
  it('matches case-insensitively, across items, and maps back to item segments', () => {
    const pages = [
      [{ str: 'Total due:' }, { str: ' 450 MAD', hasEOL: true }, { str: 'Thank you' }],
      [{ str: 'no match here' }],
      [{ str: 'TOTAL' }, { str: 'S' }],
    ];
    const m = findInPages(pages, 'total');
    expect(m.map((x) => x.page)).toEqual([0, 2]);
    expect(m[0].segments).toEqual([{ item: 0, start: 0, end: 5 }]);
    const across = findInPages(pages, 'due: 450');
    expect(across[0].segments).toEqual([
      { item: 0, start: 6, end: 10 },
      { item: 1, start: 0, end: 4 },
    ]);
    expect(findInPages(pages, 'mad thank')[0].segments.map((s) => s.item)).toEqual([1, 2]);
    expect(findInPages(pages, '   ')).toEqual([]);
  });
});
