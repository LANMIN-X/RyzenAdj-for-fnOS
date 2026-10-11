const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/ui/app.js'), 'utf8');
const start = source.indexOf('async function checkOtherBaseline(');
const end = source.indexOf('\n$("low-power-cancel")', start);

(async () => {
  for (const [temperature, different, choice, result, restore] of [
    [false, false, '', true, null], [false, true, 'skip', true, null],
    [false, true, 'apply', true, '/api/temperature/restore'],
    [true, true, 'apply', true, '/api/power/restore'], [true, true, '', false, null],
  ]) {
    const calls = [];
    const state = { temperature_limit: different ? 28 : 92, baseline_temperature: 92.001,
      limits: { stapm: different ? 12 : 45 }, baseline: { stapm: 45 }, power_limit_support: { stapm: true } };
    const context = vm.createContext({ LIMIT_KEYS: ['stapm'], LIMIT_LABELS: { stapm: 'STAPM' },
      watts: (v) => v.toFixed(1), refresh: async () => {},
      confirmSetting: async () => choice,
      request: async (url) => { calls.push(url); return state; } });
    vm.runInContext(source.slice(start, end), context);
    assert.equal(await context.checkOtherBaseline(temperature), result);
    assert.deepEqual(calls, restore ? ['/api/status', restore] : ['/api/status']);
    state.baseline_temperature = null;
    await assert.rejects(context.checkOtherBaseline(false), /未应用本次设置/);
  }
})();
