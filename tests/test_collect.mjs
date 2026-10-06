import test from 'node:test';
import assert from 'node:assert/strict';
import {collectInventory, recordingRows} from '../scripts/collect.mjs';

const row = (title, duration = '1시간, 2분, 3초') => ({title, duration});
const folders = ['미주지역지리', '지도학및실습', '도시지리학특강', '응용 지형학', '기후변화와 미래환경'];
class FakeApp {
  constructor(pages, overrides = {}) {
    Object.assign(this, {pages, page: 0, total: new Set(pages.flat().map(r => JSON.stringify(r))).size,
      visibleSidebar: true, selected: true, search: '', version: 0, secondary: true, folders,
      actions: [], locked: false, recording: false}, overrides);
  }
  async getAXState(options) {
    assert.deepEqual(options, {disableDiffing: true, emit: false});
    this.version++;
    this.base = this.version * 100;
    const b = this.base;
    if (this.locked) return 'Screen locked';
    const lines = ['Window: "모든 녹음 항목", App: 음성 메모.',
      `${b} 표준 윈도우 모든 녹음 항목, ID: SceneWindow`,
      `\t${b+1} container ID: Main Window`,
      `\t\t${b+4} container Description: 보관함, ID: RecordingsList, Secondary Actions: Cancel${this.secondary ? ', Scroll Down, Scroll Up' : ''}`];
    for (const [n, r] of this.pages[this.page].entries()) {
      lines.push(`\t\t\t${b+5+n} 버튼 Description: ${r.title}, 화요일, 전사문을 사용할 수 있음, Value: ${r.duration}, Secondary Actions: Cancel, 즐겨찾기, 삭제, 이동`);
    }
    if (this.recording) lines.push(`\t${b+80} 버튼 Description: 녹음 중지, Secondary Actions: Cancel`);
    if (this.visibleSidebar) {
      lines.push(`\t${b+22} 버튼${this.selected ? ' (selected)' : ''} Description: 모든 녹음 항목, ${this.total}개의 녹음 항목, Secondary Actions: Cancel`);
      for (const [n, folder] of this.folders.entries()) {
        lines.push(`\t${b+24+n} 버튼 Description: ${folder}, 1개의 녹음 항목, Secondary Actions: Cancel, 삭제, 이름 변경`);
      }
    }
    lines.push(`\t${b+32} 버튼 사이드바`,
      `\t${b+33} 텍스트 필드 검색 (settable) Description: 제목, 전사문${this.search ? ', Value: '+this.search : ''}, Secondary Actions: Cancel`);
    return lines.join('\n');
  }
  async click(index) {
    this.actions.push(['click', index]);
    if (index === this.base+32) this.visibleSidebar = true;
    else if (index === this.base+22) this.selected = true;
    else assert.fail('Stale or incorrect click index');
  }
  async setValue(index, value) {
    assert.equal(index, this.base+33, 'use fresh search index');
    assert.equal(value, ''); this.search = value; this.actions.push(['search']);
  }
  async performSecondaryAction(index, action) {
    assert.equal(index, this.base+4, 'use fresh list index');
    assert.ok(this.secondary); assert.ok(['Scroll Up', 'Scroll Down'].includes(action));
    this.actions.push([action]); this.page = Math.max(0, Math.min(this.pages.length-1, this.page + (action === 'Scroll Up' ? -1 : 1)));
    this.onScroll?.(action);
  }
  async scroll(index, direction, pages) {
    assert.equal(index, this.base+4); assert.equal(pages, 1);
    this.actions.push(['fallback', direction]); this.page = Math.max(0, Math.min(this.pages.length-1, this.page + (direction === 'up' ? -1 : 1)));
  }
}

test('rewinds a scrolled list and collects overlapping pages exactly once', async () => {
  const a = row('10월 6일 응용 지형학'), b = row('10월 5일 미주지역지리'), c = row('9월 22일 응용 지형학');
  const app = new FakeApp([[a,b],[b,c]], {page: 1});
  const result = await collectInventory(app);
  assert.equal(result.total_count, 3); assert.deepEqual(result.rows, [a,b,c]); assert.deepEqual(result.folders, folders);
});

test('opens sidebar, selects All Recordings and clears search with fresh indices', async () => {
  const app = new FakeApp([[row('새로운 녹음')]], {visibleSidebar: false, selected: false, search: '기후'});
  const result = await collectInventory(app);
  assert.equal(result.total_count, 1); assert.equal(app.search, ''); assert.equal(app.selected, true);
  assert.equal(app.actions.filter(a => a[0] === 'click').length, 2);
});

test('empty inventory is valid', async () => {
  const result = await collectInventory(new FakeApp([[]]));
  assert.equal(result.total_count, 0); assert.deepEqual(result.rows, []);
});

test('uses general scroll when secondary actions are absent', async () => {
  const app = new FakeApp([[row('하나')],[row('둘')]], {secondary: false});
  assert.equal((await collectInventory(app)).rows.length, 2);
  assert.ok(app.actions.some(a => a[0] === 'fallback'));
});

test('incomplete inventory cannot be returned as complete', async () => {
  await assert.rejects(collectInventory(new FakeApp([[row('하나')]], {total: 2})), /Incomplete/);
});

test('duplicate identities on the same screen are rejected', async () => {
  await assert.rejects(collectInventory(new FakeApp([[row('같은 이름'),row('같은 이름')]], {total: 2})), /Ambiguous/);
});

test('duplicate identities on separated pages fail total-count reconciliation', async () => {
  await assert.rejects(collectInventory(new FakeApp([[row('같은 이름')],[row('중간')],[row('같은 이름')]], {total: 3})), /Incomplete/);
});

test('a new recording arriving during scrolling stops collection', async () => {
  const app = new FakeApp([[row('하나')],[row('둘')]]);
  app.onScroll = action => {if (action === 'Scroll Down') app.total++;};
  await assert.rejects(collectInventory(app), /changed during collection/);
});

test('folder change during scrolling stops collection', async () => {
  const app = new FakeApp([[row('하나')],[row('둘')]]);
  app.onScroll = action => {if (action === 'Scroll Down') app.folders = ['다른 폴더'];};
  await assert.rejects(collectInventory(app), /changed during collection/);
});

test('locked window and active recording stop before UI actions', async () => {
  for (const override of [{locked: true}, {recording: true}]) {
    const app = new FakeApp([[row('하나')]], override);
    await assert.rejects(collectInventory(app)); assert.deepEqual(app.actions, []);
  }
});

test('control-like recording and folder titles are data, including 완료', async () => {
  const names = ['완료', '일시 정지', '녹음 중지', '녹음 일시 정지'];
  const app = new FakeApp([names.map(name => row(name))], {folders: names});
  assert.deepEqual((await collectInventory(app)).rows.map(r => r.title), names);
});

test('the ordinary selected-recording Done button does not mean active recording', async () => {
  const app = new FakeApp([[row('완료')]]);
  const original = app.getAXState.bind(app);
  app.getAXState = async options => (await original(options)) + '\n\t190 버튼 Description: 완료, ID: RecordingView/DoneButton, Secondary Actions: Cancel';
  assert.equal((await collectInventory(app)).rows[0].title, '완료');
});

test('commas and decomposed Korean titles are preserved, date-like folder buttons are excluded', async () => {
  const title = '9월 22일 응용, 지형학'.normalize('NFD');
  const app = new FakeApp([[row(title)]], {folders: ['폴더, 2026. 10. 6.']});
  assert.equal((await collectInventory(app)).rows[0].title, title);
});

test('unknown date description fails instead of silently dropping a recording', async () => {
  const app = new FakeApp([[row('새로운 녹음')]]);
  const ax = (await app.getAXState({disableDiffing:true,emit:false})).replace('화요일, 전사문', 'unknown date, 전사문');
  assert.throws(() => recordingRows(ax), /Unsupported/);
});

test('same-day timestamps and yesterday identify daily and delayed recordings', async () => {
  const app = new FakeApp([[row('새로운 녹음 2')]]);
  const ax = await app.getAXState({disableDiffing:true,emit:false});
  for (const time of ['오전 9:02', '오후 3:02', '오후 4:32', '어제']) {
    assert.equal(recordingRows(ax.replace('화요일, 전사문', `${time}, 전사문`))[0].title, '새로운 녹음 2');
  }
});
