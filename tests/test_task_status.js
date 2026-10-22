const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const helper = source.slice(source.indexOf('function taskDetailText('), source.indexOf('function showTask('));
const context = vm.createContext({formatDuration: value => `${value}秒`});
vm.runInContext(helper, context);
test('分段、复用和无进展提示同时显示', () => {
  const text = context.taskDetailText({state:'running', phase:'正在处理第 3 / 10 段',
    cachedBatches:2, processedSeconds:12, expectedSeconds:100, stalledSeconds:45});
  assert.match(text, /第 3 \/ 10 段/);
  assert.match(text, /已复用 2 段/);
  assert.match(text, /45秒.*没有新增画面/);
});
test('完成及取消后不显示停滞告警', () => {
  for (const state of ['completed','cancelled']) {
    assert.doesNotMatch(context.taskDetailText({state, stalledSeconds:99}), /没有新增画面/);
  }
  assert.equal(context.taskDetailText({state:'failed', error:'编码失败'}), '编码失败');
});
