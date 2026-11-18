// Verify Dashboard pagination uses the selected locale without translating catalog data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let locale = 'en';
const context = vm.createContext({
  window: {
    addEventListener() {},
    skillHubText: (english, vietnamese) => locale === 'vi' ? vietnamese : english,
  },
});

vm.runInContext(fs.readFileSync(path.join(__dirname, 'static/app.js'), 'utf8'), context);
assert.equal(vm.runInContext('dashboardPage(3)', context), 'Page 3');
locale = 'vi';
assert.equal(vm.runInContext('dashboardPage(3)', context), 'Trang 3');
console.log('Dashboard pagination switches between English and Vietnamese');
