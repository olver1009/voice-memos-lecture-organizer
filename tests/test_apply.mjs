import test from 'node:test';
import assert from 'node:assert/strict';
import {applyActions} from '../scripts/apply.mjs';

const source = '미주지역지리', destination = '응용 지형학';
const action = (id = 1, overrides = {}) => ({z_pk: id, current_title: `새로운 녹음 ${id}`,
  desired_title: `10월 6일 응용 지형학 ${id}-2`, duration_seconds: 4000,
  rename_to: `10월 6일 응용 지형학 ${id}-2`, move_to: destination, course: destination, ...overrides});

class FakeApp {
  constructor(actions, overrides = {}) {
    this.rows = actions.map(a => ({title: a.current_title, duration: a.duration_seconds, folder: a.move_to ? source : destination}));
    Object.assign(this, {version: 0, search: '', folder: '모든 녹음 항목', selected: null,
      sheet: false, mutations: [], pending: null, ignoreMove: false}, overrides);
  }
  async getAXState(options) {
    assert.deepEqual(options, {disableDiffing: true, emit: false});
    this.base = ++this.version * 100;
    const b = this.base;
    if (this.sheet) return `${b} 시트\n\t${b+1} container 폴더 선택, ID: FoldersList\n\t\t${b+2} 버튼 Description: ${destination}, 0개의 녹음 항목, Secondary Actions: Cancel`;
    this.visible = this.rows.filter(r => r.title.includes(this.search) && (this.folder === '모든 녹음 항목' || r.folder === this.folder));
    if (this.hiddenSelectionAfterClear && !this.search && this.folder === '모든 녹음 항목' && !this.selectedVisible) {
      this.visible = this.visible.filter(r => r !== this.selected);
    }
    const lines = [`${b} 표준 윈도우 ID: SceneWindow`, `\t${b+1} container ID: Main Window`,
      `\t\t${b+4} container ID: RecordingsList, Secondary Actions: Cancel, Scroll Up, Scroll Down`];
    for (const [n,r] of this.visible.entries()) lines.push(`\t\t\t${b+5+n} 버튼${this.selected === r ? ' (selected)' : ''} Description: ${r.oldLabel ?? r.title}, 화요일, Value: 1시간, 6분, 40초, Secondary Actions: Cancel, 이동`);
    lines.push(`\t${b+20} 버튼${this.folder === '모든 녹음 항목' ? ' (selected)' : ''} Description: 모든 녹음 항목, ${this.rows.length}개의 녹음 항목, Secondary Actions: Cancel`,
      `\t${b+21} 버튼${this.folder === destination ? ' (selected)' : ''} Description: ${destination}, 1개의 녹음 항목, Secondary Actions: Cancel`,
      `\t${b+22} 텍스트 필드 검색 (settable) Description: 제목, 전사문${this.search ? ', Value: '+this.search : ''}, Secondary Actions: Cancel`);
    if (this.selected) lines.push(`\t${b+23} 텍스트 필드 (settable) Value: ${this.wrongField ? '다른 녹음' : this.selected.title}, Secondary Actions: Cancel`,
      `\t${b+24} 버튼 Description: 완료, ID: RecordingView/DoneButton, Secondary Actions: Cancel`);
    return lines.join('\n');
  }
  async setValue(index, value) {
    if (index === this.base+22) {if (this.search && !value) this.selectedVisible = false; this.search = value;}
    else if (index === this.base+23) {assert.ok(this.selected); this.pending = value;}
    else assert.fail('Stale or wrong field index');
  }
  async pressKey(key) {
    assert.equal(key, 'Return'); assert.ok(this.pending);
    assert.equal(this.focus, 'title', 'commit in the actual title field');
    this.mutations.push(['rename', this.selected.title, this.pending]);
    if (this.labelLag) this.selected.oldLabel = this.selected.title;
    this.selected.title = this.pending; this.pending = null;
  }
  async click(index) {
    if (this.sheet) {
      assert.equal(index, this.base+2, 'fresh sheet folder index');
      if (!this.ignoreMove) this.selected.folder = destination;
      this.mutations.push(['move', this.selected.title]); this.sheet = false;
    } else if (index === this.base+23) {this.focus = 'title'; if (this.focusResetsList) this.selectedVisible = false;}
    else if (index === this.base+20) this.folder = '모든 녹음 항목';
    else if (index === this.base+21) {this.folder = destination; for (const r of this.rows) delete r.oldLabel;}
    else {assert.ok(index >= this.base+5 && index < this.base+5+this.visible.length, 'fresh row index'); this.selected = this.visible[index-this.base-5];}
  }
  async performSecondaryAction(index, name) {
    if (name.startsWith('Scroll ')) {
      assert.equal(index, this.base+4, 'fresh list index');
      this.selectedVisible = name === 'Scroll Down'; return;
    }
    assert.equal(name, '이동');
    assert.equal(this.visible[index-this.base-5], this.selected, 'move the selected row with a fresh index');
    this.sheet = true;
  }
}

test('rename, move, folder verification and All Recordings return use fresh indices', async () => {
  const actions = [action(1), action(2)]; const app = new FakeApp(actions);
  const result = await applyActions(app, {status:'ok', actions});
  assert.equal(result.status, 'ok'); assert.equal(result.ui_completed.length, 2);
  assert.deepEqual(app.rows.map(r => [r.title, r.folder]), actions.map(a => [a.desired_title, destination]));
  assert.equal(app.folder, '모든 녹음 항목'); assert.equal(app.search, '');
  assert.equal(app.mutations.length, 4);
});

test('rename-only and move-only do not perform the other operation', async () => {
  for (const a of [action(1, {move_to:null}), action(1, {rename_to:null, desired_title:'새로운 녹음 1'})]) {
    const app = new FakeApp([a]); const result = await applyActions(app, {status:'ok', actions:[a]});
    assert.equal(result.status, 'ok'); assert.equal(app.mutations.length, 1);
    assert.equal(app.mutations[0][0], a.rename_to ? 'rename' : 'move');
  }
});

test('a saved title with a stale AX list label is verified after rebuilding the folder view', async () => {
  const actions = [action(1)]; const app = new FakeApp(actions, {labelLag:true});
  assert.equal((await applyActions(app, {status:'ok', actions})).status, 'ok');
  assert.equal(app.rows[0].title, actions[0].desired_title);
  assert.equal(app.rows[0].oldLabel, undefined);
});

test('an older selected recording is located by scrolling after clearing search', async () => {
  const actions = [action(1)]; const app = new FakeApp(actions, {hiddenSelectionAfterClear:true, focusResetsList:true, labelLag:true});
  assert.equal((await applyActions(app, {status:'ok', actions})).status, 'ok');
  assert.equal(app.rows[0].folder, destination);
});

test('ambiguous rows or incorrect selected title stop before mutation', async () => {
  for (const override of [{wrongField:true}, {rows:[{title:'새로운 녹음 1',folder:source},{title:'새로운 녹음 1',folder:source}]}]) {
    const app = new FakeApp([action()], override);
    assert.equal((await applyActions(app, {status:'ok', actions:[action()]})).status, 'failed');
    assert.deepEqual(app.mutations, []);
  }
});

test('a move that was not saved fails destination verification without repeating it', async () => {
  const actions = [action(1), action(2)]; const app = new FakeApp(actions, {ignoreMove:true});
  const result = await applyActions(app, {status:'ok', actions});
  assert.equal(result.status, 'failed'); assert.equal(result.failed_z_pk, 1);
  assert.equal(app.mutations.filter(m => m[0] === 'move').length, 1);
  assert.equal(app.rows[1].title, actions[1].current_title);
});

test('partial failure reports earlier work and stops subsequent actions', async () => {
  const actions = [action(1), action(2), action(3)]; const app = new FakeApp(actions);
  const click = app.click.bind(app);
  app.click = async index => {await click(index); if (app.selected?.title === '새로운 녹음 2') app.wrongField = true;};
  const result = await applyActions(app, {status:'ok', actions});
  assert.equal(result.status, 'failed'); assert.equal(result.failed_z_pk, 2);
  assert.deepEqual(result.ui_completed, [{z_pk:1, renamed:true, moved:true}]);
  assert.equal(app.rows[2].title, actions[2].current_title);
});

test('no-op and fatal plans never touch the app', async () => {
  const app = {getAXState() {assert.fail('no UI call expected');}};
  assert.equal((await applyActions(app, {status:'ok', actions:[]})).status, 'ok');
  assert.equal((await applyActions(app, {status:'fatal', actions:[action()]})).status, 'failed');
});


test('a transient dismissed sheet is observed without repeating the saved move', async () => {
  const actions = [action(1), action(2)]; const app = new FakeApp(actions);
  const click = app.click.bind(app), observe = app.getAXState.bind(app);
  let transient = 0;
  app.click = async index => {const moving = app.sheet; await click(index); if (moving) transient = 2;};
  app.getAXState = async options => transient-- > 0
    ? '0 シート 폴더 선택, ID: FoldersList' : observe(options);
  const result = await applyActions(app, {status:'ok', actions});
  assert.equal(result.status, 'ok'); assert.equal(result.ui_completed.length, 2);
  assert.equal(app.mutations.filter(m => m[0] === 'move').length, 2);
});

test('a permanent missing window after a saved move stops with attempted work and stage', async () => {
  const actions = [action(1), action(2)]; const app = new FakeApp(actions);
  const click = app.click.bind(app), observe = app.getAXState.bind(app);
  let unavailable = false, observations = 0;
  app.click = async index => {const moving = app.sheet; await click(index); if (moving) unavailable = true;};
  app.getAXState = async options => {
    if (unavailable) {observations++; return 'Screen locked';}
    return observe(options);
  };
  const result = await applyActions(app, {status:'ok', actions});
  assert.equal(result.status, 'failed'); assert.equal(observations, 5);
  assert.equal(result.stage, 'after folder move');
  assert.deepEqual(result.attempted, [{z_pk:1, rename_attempted:true, move_attempted:true}]);
  assert.equal(app.rows[0].title, actions[0].desired_title);
  assert.equal(app.rows[1].title, actions[1].current_title);
  assert.equal(app.mutations.filter(m => m[0] === 'move').length, 1);
});

test('a delayed destination view is observed without repeating the folder click', async () => {
  const actions = [action(1)]; const app = new FakeApp(actions);
  const click = app.click.bind(app), observe = app.getAXState.bind(app);
  let lag = 0;
  app.click = async index => {const switching = !app.sheet && index === app.base+21; await click(index); if (switching) lag = 1;};
  app.getAXState = async options => {
    const state = await observe(options);
    return lag-- > 0 ? state.replace(/버튼 \(selected\) Description: 응용 지형학/g, '버튼 Description: 응용 지형학') : state;
  };
  assert.equal((await applyActions(app, {status:'ok', actions})).status, 'ok');
  assert.equal(app.mutations.length, 2);
});
