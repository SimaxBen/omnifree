import { describe, expect, it } from 'vitest';
import { normalizeMarkdown, parseBlocks, parseInline, plainText, splitRow, toggleCheckbox, type Block } from '../markdown';

// The note an AI chat produced: GFM tables with bold cells, --- rules, `*` bullets with trailing double
// spaces, numbered steps under ### headings and unicode non-breaking hyphens (U+2011).
const RECIPE = [
  '### Tajine de poulet aux citrons confits',
  '',
  '**Pour 4 personnes** · Préparation : 20 min  ',
  '**Cuisson** : 1 h 15',
  '',
  '---',
  '',
  '### Ingrédients',
  '',
  '| Ingrédient | Quantité | Remarque |',
  '|---|---:|:---:|',
  '| **Poulet** fermier | 1,5 kg | coupé en 8 |',
  '| Citrons confits | 2 | non‑traités |',
  '| Olives violettes | 150 g | dénoyautées |',
  '',
  '---',
  '',
  '### Épices',
  '',
  '* Gingembre moulu : 1 c. à café  ',
  '* Curcuma : ½ c. à café  ',
  '* Safran : une pincée  ',
  '',
  '### Préparation',
  '',
  '1. Mélanger les épices avec l’huile.  ',
  '2. Faire revenir le poulet **5 min** de chaque côté.',
  '3. Ajouter les citrons ; cuire à feu doux.',
  '',
  '### Service',
  '',
  '4. Parsemer de coriandre.',
  '5. Servir avec du pain.',
  '',
  '> Astuce : ajoutez les olives en fin de cuisson.',
].join('\n');

const kinds = (blocks: Block[]) => blocks.map((b) => b.t);

describe('markdown: the AI recipe note', () => {
  const blocks = parseBlocks(RECIPE);

  it('splits it into the expected blocks', () => {
    expect(kinds(blocks)).toEqual([
      'heading', 'para', 'hr', 'heading', 'table', 'hr', 'heading', 'list', 'heading', 'list', 'heading', 'list', 'quote',
    ]);
    expect(blocks[0]).toEqual({ t: 'heading', level: 3, text: 'Tajine de poulet aux citrons confits' });
  });

  it('keeps the hard break and the bold in the intro paragraph', () => {
    expect(blocks[1]).toEqual({ t: 'para', text: '**Pour 4 personnes** · Préparation : 20 min\n**Cuisson** : 1 h 15' });
    expect(parseInline((blocks[1] as { text: string }).text)).toEqual([
      { t: 'strong', c: [{ t: 'text', v: 'Pour 4 personnes' }] },
      { t: 'text', v: ' · Préparation : 20 min' },
      { t: 'br' },
      { t: 'strong', c: [{ t: 'text', v: 'Cuisson' }] },
      { t: 'text', v: ' : 1 h 15' },
    ]);
  });

  it('parses the GFM table with alignment, bold cells and plain hyphens', () => {
    expect(blocks[4]).toEqual({
      t: 'table',
      align: [null, 'right', 'center'],
      head: ['Ingrédient', 'Quantité', 'Remarque'],
      rows: [
        ['**Poulet** fermier', '1,5 kg', 'coupé en 8'],
        ['Citrons confits', '2', 'non-traités'],
        ['Olives violettes', '150 g', 'dénoyautées'],
      ],
    });
    expect(parseInline('**Poulet** fermier')).toEqual([{ t: 'strong', c: [{ t: 'text', v: 'Poulet' }] }, { t: 'text', v: ' fermier' }]);
  });

  it('parses `*` bullets without the trailing spaces', () => {
    expect(blocks[7]).toEqual({
      t: 'list',
      ordered: false,
      start: 1,
      items: [
        { text: 'Gingembre moulu : 1 c. à café', checked: null, children: [] },
        { text: 'Curcuma : ½ c. à café', checked: null, children: [] },
        { text: 'Safran : une pincée', checked: null, children: [] },
      ],
    });
  });

  it('keeps the step numbers, also when a list continues under the next heading', () => {
    expect(blocks[9]).toMatchObject({ t: 'list', ordered: true, start: 1 });
    expect((blocks[9] as { items: { text: string }[] }).items.map((i) => i.text)).toEqual([
      'Mélanger les épices avec l’huile.',
      'Faire revenir le poulet **5 min** de chaque côté.',
      'Ajouter les citrons ; cuire à feu doux.',
    ]);
    expect(blocks[11]).toMatchObject({ t: 'list', ordered: true, start: 4, items: [{ text: 'Parsemer de coriandre.' }, { text: 'Servir avec du pain.' }] });
  });

  it('ends with the tip as a blockquote', () => {
    expect(blocks[12]).toEqual({ t: 'quote', blocks: [{ t: 'para', text: 'Astuce : ajoutez les olives en fin de cuisson.' }] });
  });

  it('previews as plain text without table or rule markup', () => {
    const preview = plainText(RECIPE, 400);
    expect(preview).toContain('Tajine de poulet aux citrons confits Pour 4 personnes');
    expect(preview).toContain('Ingrédient Quantité Remarque Poulet fermier 1,5 kg coupé en 8');
    expect(preview).not.toMatch(/[|*#]|---/);
  });
});

describe('markdown: blocks', () => {
  it('reads headings h1 to h4, deeper ones as h4, closing hashes dropped', () => {
    expect(parseBlocks('# A\n## B\n### C\n#### D\n##### E\n###### F ##')).toEqual([
      { t: 'heading', level: 1, text: 'A' },
      { t: 'heading', level: 2, text: 'B' },
      { t: 'heading', level: 3, text: 'C' },
      { t: 'heading', level: 4, text: 'D' },
      { t: 'heading', level: 4, text: 'E' },
      { t: 'heading', level: 4, text: 'F' },
    ]);
    expect(parseBlocks('#hashtag')).toEqual([{ t: 'para', text: '#hashtag' }]);
  });

  it('reads horizontal rules in every spelling', () => {
    for (const hr of ['---', '***', '___', '- - -', '* * *', '-----', '  ---  ']) {
      expect(parseBlocks(`a\n\n${hr}\n\nb`).map((b) => b.t)).toEqual(['para', 'hr', 'para']);
    }
    expect(parseBlocks('text\n---')).toEqual([{ t: 'para', text: 'text' }, { t: 'hr' }]); // no setext headings
  });

  it('reads -, * and + bullets, each kind its own list', () => {
    const blocks = parseBlocks('- a\n- b\n* c\n+ d\n+ e');
    expect(blocks.map((b) => (b as { items: { text: string }[] }).items.map((i) => i.text))).toEqual([['a', 'b'], ['c'], ['d', 'e']]);
  });

  it('keeps ordered numbers and accepts 1) markers', () => {
    expect(parseBlocks('3. c\n4. d')).toEqual([
      { t: 'list', ordered: true, start: 3, items: [{ text: 'c', checked: null, children: [] }, { text: 'd', checked: null, children: [] }] },
    ]);
    expect(parseBlocks('1) a\n2) b')[0]).toMatchObject({ ordered: true, start: 1, items: [{ text: 'a' }, { text: 'b' }] });
  });

  it('nests lists by indentation', () => {
    const [list] = parseBlocks('- fruits\n  - apples\n  - pears\n    1. conference\n- veg\n\t- leeks');
    expect(list).toEqual({
      t: 'list',
      ordered: false,
      start: 1,
      items: [
        {
          text: 'fruits',
          checked: null,
          children: [
            {
              t: 'list',
              ordered: false,
              start: 1,
              items: [
                { text: 'apples', checked: null, children: [] },
                {
                  text: 'pears',
                  checked: null,
                  children: [{ t: 'list', ordered: true, start: 1, items: [{ text: 'conference', checked: null, children: [] }] }],
                },
              ],
            },
          ],
        },
        { text: 'veg', checked: null, children: [{ t: 'list', ordered: false, start: 1, items: [{ text: 'leeks', checked: null, children: [] }] }] },
      ],
    });
  });

  it('nests bullets under numbered steps (3-space indent) and keeps loose lists together', () => {
    const blocks = parseBlocks('1. Prep\n   * wash\n   * cut\n\n2. Cook\n\n   Stir often.\n\n3. Serve');
    expect(blocks).toHaveLength(1);
    const list = blocks[0] as Extract<Block, { t: 'list' }>;
    expect(list.items.map((i) => i.text)).toEqual(['Prep', 'Cook', 'Serve']);
    expect(list.items[0].children[0]).toMatchObject({ t: 'list', ordered: false, items: [{ text: 'wash' }, { text: 'cut' }] });
    expect(list.items[1].children).toEqual([{ t: 'para', text: 'Stir often.' }]);
  });

  it('continues a list item on a lazy line, and ends it at a paragraph after a blank line', () => {
    expect(parseBlocks('- first line\nsecond line\n\nafter')).toEqual([
      { t: 'list', ordered: false, start: 1, items: [{ text: 'first line\nsecond line', checked: null, children: [] }] },
      { t: 'para', text: 'after' },
    ]);
  });

  it('does not start an ordered list mid-paragraph unless it starts at 1', () => {
    expect(parseBlocks('In\n2026. was a year')).toEqual([{ t: 'para', text: 'In\n2026. was a year' }]);
    expect(parseBlocks('Steps:\n1. go')).toMatchObject([{ t: 'para' }, { t: 'list', ordered: true }]);
  });

  it('reads task items, nested ones too', () => {
    const [list] = parseBlocks('- [ ] milk\n- [x] eggs\n  - [X] free range\n- [ ]\n- [x]no space');
    const items = (list as Extract<Block, { t: 'list' }>).items;
    expect(items.map((i) => [i.text, i.checked])).toEqual([['milk', false], ['eggs', true], ['', false], ['[x]no space', null]]);
    expect(items[1].children[0]).toMatchObject({ items: [{ text: 'free range', checked: true }] });
  });

  it('reads blockquotes with blocks inside, and lazy quote lines', () => {
    expect(parseBlocks('> **Note**\n> - one\n> - two\n\n> a\nlazy')).toEqual([
      {
        t: 'quote',
        blocks: [
          { t: 'para', text: '**Note**' },
          { t: 'list', ordered: false, start: 1, items: [{ text: 'one', checked: null, children: [] }, { text: 'two', checked: null, children: [] }] },
        ],
      },
      { t: 'quote', blocks: [{ t: 'para', text: 'a\nlazy' }] },
    ]);
  });

  it('reads fenced code with its language, ``` or ~~~, unclosed until the end', () => {
    expect(parseBlocks('```python\nprint("a | b")\n\n# not a heading\n```\nafter')).toEqual([
      { t: 'code', lang: 'python', text: 'print("a | b")\n\n# not a heading' },
      { t: 'para', text: 'after' },
    ]);
    expect(parseBlocks('~~~\n- not a list\n~~~')).toEqual([{ t: 'code', lang: '', text: '- not a list' }]);
    expect(parseBlocks('````\n```\ninner\n```\n````')).toEqual([{ t: 'code', lang: '', text: '```\ninner\n```' }]);
    expect(parseBlocks('text\n```js\nlet a = 1;')).toEqual([{ t: 'para', text: 'text' }, { t: 'code', lang: 'js', text: 'let a = 1;' }]);
    expect(parseBlocks('- item\n\n  ```\n  code\n  ```')[0]).toMatchObject({ items: [{ text: 'item', children: [{ t: 'code', text: 'code' }] }] });
  });
});

describe('markdown: tables', () => {
  it('splits rows on unescaped pipes outside inline code', () => {
    expect(splitRow('| a | b \\| c | `x | y` |')).toEqual(['a', 'b | c', '`x | y`']);
    expect(splitRow('a|b')).toEqual(['a', 'b']);
    expect(splitRow('| only |')).toEqual(['only']);
  });

  it('accepts tables without outer pipes and pads short rows', () => {
    expect(parseBlocks('Name | Qty\n--- | ---\nMilk | 2\nEggs |')).toEqual([
      { t: 'table', align: [null, null], head: ['Name', 'Qty'], rows: [['Milk', '2'], ['Eggs', '']] },
    ]);
  });

  it('trims extra cells and stops at a blank or pipe-less line', () => {
    expect(parseBlocks('| a | b |\n|:--|--|\n| 1 | 2 | 3 |\nnot a row\n')).toEqual([
      { t: 'table', align: ['left', null], head: ['a', 'b'], rows: [['1', '2']] },
      { t: 'para', text: 'not a row' },
    ]);
  });

  it('can interrupt a paragraph, and ignores lookalikes', () => {
    expect(kinds(parseBlocks('Intro\n| a | b |\n|---|---|\n| 1 | 2 |'))).toEqual(['para', 'table']);
    expect(kinds(parseBlocks('a | b'))).toEqual(['para']);
    expect(kinds(parseBlocks('a | b\n---'))).toEqual(['para', 'hr']);
    expect(kinds(parseBlocks('| a |\n| b |'))).toEqual(['para']);
  });
});

describe('markdown: inline', () => {
  it('reads strike, double-backtick code, strong+em and escapes', () => {
    expect(parseInline('~~old~~ new')).toEqual([{ t: 'strike', c: [{ t: 'text', v: 'old' }] }, { t: 'text', v: ' new' }]);
    expect(parseInline('`` a ` b ``')).toEqual([{ t: 'code', v: 'a ` b' }]);
    expect(parseInline('***both***')).toEqual([{ t: 'strong', c: [{ t: 'em', c: [{ t: 'text', v: 'both' }] }] }]);
    expect(parseInline('\\*not em\\* 5 \\| 6')).toEqual([{ t: 'text', v: '*not em* 5 | 6' }]);
    expect(parseInline('__bold__ and _em_')).toEqual([
      { t: 'strong', c: [{ t: 'text', v: 'bold' }] },
      { t: 'text', v: ' and ' },
      { t: 'em', c: [{ t: 'text', v: 'em' }] },
    ]);
    expect(parseInline('2 * 3 * 4')).toEqual([{ t: 'text', v: '2 * 3 * 4' }]);
  });

  it('reads links safely: titles, autolinks, bare URLs; never javascript:', () => {
    expect(parseInline('[site](https://a.b/c "Title")')).toEqual([{ t: 'link', href: 'https://a.b/c', c: [{ t: 'text', v: 'site' }] }]);
    expect(parseInline('<https://a.b> <mailto:me@a.b>')).toEqual([
      { t: 'link', href: 'https://a.b', c: [{ t: 'text', v: 'https://a.b' }] },
      { t: 'text', v: ' ' },
      { t: 'link', href: 'mailto:me@a.b', c: [{ t: 'text', v: 'me@a.b' }] },
    ]);
    expect(parseInline('[x](javascript:alert(1))').some((n) => n.t === 'link')).toBe(false);
    expect(parseInline('[x](javascript:alert)')).toEqual([{ t: 'text', v: 'x' }]);
    expect(parseInline('[x](data:text/html,hi)')).toEqual([{ t: 'text', v: 'x' }]);
    expect(parseInline('<script>alert(1)</script>')).toEqual([{ t: 'text', v: '<script>alert(1)</script>' }]); // shown as text
    expect(parseInline('**https://a.b/c**')).toEqual([{ t: 'strong', c: [{ t: 'link', href: 'https://a.b/c', c: [{ t: 'text', v: 'https://a.b/c' }] }] }]);
  });

  it('turns line breaks into br tokens, with or without trailing spaces or a backslash', () => {
    expect(parseInline('a  \nb\\\nc\nd')).toEqual([
      { t: 'text', v: 'a' },
      { t: 'br' },
      { t: 'text', v: 'b' },
      { t: 'br' },
      { t: 'text', v: 'c' },
      { t: 'br' },
      { t: 'text', v: 'd' },
    ]);
  });

  it('normalizes non-breaking hyphens, CRLF and tabs', () => {
    expect(normalizeMarkdown('non‑breaking‐x\r\n\tcode​')).toBe('non-breaking-x\n    code');
    expect(parseBlocks('a‑b')).toEqual([{ t: 'para', text: 'a-b' }]);
  });
});

describe('markdown: checkboxes', () => {
  it('toggles in document order, nested and quoted ones included, code skipped', () => {
    const md = '- [ ] a\n  - [x] a1\n```\n- [ ] not a box\n```\n> - [ ] quoted\n1. [ ] numbered';
    expect(toggleCheckbox(md, 0)).toBe(md.replace('- [ ] a', '- [x] a'));
    expect(toggleCheckbox(md, 1)).toBe(md.replace('- [x] a1', '- [ ] a1'));
    expect(toggleCheckbox(md, 2)).toBe(md.replace('> - [ ] quoted', '> - [x] quoted'));
    expect(toggleCheckbox(md, 3)).toBe(md.replace('1. [ ] numbered', '1. [x] numbered'));
    expect(toggleCheckbox(md, 4)).toBe(md);
    // Same count as the rendered boxes.
    const count = (blocks: Block[]): number =>
      blocks.reduce((n, b) => {
        if (b.t === 'list') return n + b.items.reduce((m, i) => m + (i.checked !== null ? 1 : 0) + count(i.children), 0);
        if (b.t === 'quote') return n + count(b.blocks);
        return n;
      }, 0);
    expect(count(parseBlocks(md))).toBe(4);
  });
});
