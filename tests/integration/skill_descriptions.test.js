'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {describe} = require('../../novamind/webui/static/skill_descriptions.js');

function* skills(dir) {
  for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* skills(file);
    else if (entry.name === 'SKILL.md') yield file;
  }
}

test('every bundled skill has a Chinese introduction in the UI', () => {
  let count = 0;
  for (const file of skills(path.join(__dirname, '../../novamind/core/skill/builtin_skills'))) {
    const frontmatter = fs.readFileSync(file, 'utf8').split('---')[1];
    const description = frontmatter.match(/^description:\s*(.+)$/m)[1].replace(/^(["'])(.*)\1$/, '$2');
    assert.match(describe(description), /[\u4e00-\u9fff]/, file);
    count++;
  }
  assert.ok(count >= 37);
});

test('custom descriptions and existing Chinese text keep their meaning', () => {
  assert.equal(describe('我自己的技能介绍'), '我自己的技能介绍');
  assert.equal(describe('A user-authored custom description'), 'A user-authored custom description');
});
