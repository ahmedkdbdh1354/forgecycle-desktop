const $ = (id) => document.getElementById(id);
let csrf = '';
let current = null;
let settings = {};
let role = 'planner';
let lastQuestions = '';
let lastApproval = '';
let historyView = false;
let toastTimer;

function toast(message, bad = false) {
  const box = $('toast');
  box.textContent = message;
  box.className = (bad ? 'error ' : '') + 'show';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.className = '', 4300);
}

async function api(path, data) {
  const response = await fetch('/api/' + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-ForgeCycle-Token': csrf },
    body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'تعذر الاتصال بالخادم.');
  return result;
}

async function action(path, data, success) {
  try {
    await api(path, data);
    if (success) toast(success);
    await poll();
  } catch (error) {
    toast(error.message, true);
  }
}

function selectTab(name) {
  document.querySelectorAll('.page').forEach(x => x.classList.toggle('active', x.id === name));
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.tab === name));
  $('page-name').textContent = { studio: 'الاستوديو', activity: 'النشاط والسجل', settings: 'الإعدادات' }[name];
  if (name === 'studio') historyView = false;
  if (name === 'activity') loadHistory();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function renderEvents(state, element, limit) {
  element.replaceChildren();
  const events = (state.events || []).slice(-limit).reverse();
  if (!events.length) {
    element.append(node('p', 'muted', 'ماكو نشاط بعد.'));
    return;
  }
  const labels = { planner: 'المنسّق', builder: 'البنّاء', reviewer: 'المراجع', system: 'النظام' };
  events.forEach(event => {
    const row = node('div', 'event ' + event.role);
    row.append(node('time', '', event.time || ''), node('b', '', labels[event.role] || event.role), node('p', '', event.text));
    element.append(row);
  });
}

function render(state) {
  current = state;
  const phases = { idle: 'جاهز', planning: 'المنسّق يعمل', questions: 'بانتظار إجاباتك', approval: 'بانتظار موافقتك', building: 'البنّاء يعمل', reviewing: 'المراجع يعمل', complete: 'مكتمل', needs_attention: 'يحتاج متابعة', stopped: 'متوقف', error: 'حدث خطأ' };
  $('phase-badge').textContent = phases[state.phase] || state.phase;
  if (state.id && $('prompt').dataset.run !== state.id) {
    $('prompt').value = state.prompt || '';
    $('prompt').dataset.run = state.id;
  }
  $('start-btn').disabled = ['planning', 'building', 'reviewing'].includes(state.phase);
  const questionActive = state.phase === 'questions';
  $('questions-card').classList.toggle('hidden', !questionActive);
  const qKey = state.id + JSON.stringify(state.questions) + String(state.answers.length);
  if (questionActive && qKey !== lastQuestions) {
    lastQuestions = qKey;
    $('questions-list').replaceChildren();
    (state.questions || []).forEach((q, i) => {
      const wrap = node('div', 'question');
      const label = node('label', '', (i + 1) + '. ' + q);
      const input = node('textarea');
      input.rows = 2;
      input.placeholder = 'اكتب إجابتك هنا...';
      input.dataset.question = String(i);
      wrap.append(label, input);
      $('questions-list').append(wrap);
    });
  }
  const approvalActive = state.phase === 'approval';
  $('approval-card').classList.toggle('hidden', !approvalActive);
  if (approvalActive && lastApproval !== state.id + state.brief) {
    lastApproval = state.id + state.brief;
    $('brief').value = state.brief || '';
    $('acceptance').value = (state.acceptance || []).join('\n');
    $('workspace').value = '';
    $('test-command').value = '';
  }
  const showProgress = Boolean(state.id && !['idle', 'questions', 'approval', 'planning'].includes(state.phase));
  $('progress-card').classList.toggle('hidden', !showProgress);
  $('progress-title').textContent = ({ building: 'البنّاء يشتغل', reviewing: 'المراجع يدقّق', complete: 'المشروع مكتمل', error: 'توقف بسبب خطأ', stopped: 'تم الإيقاف', needs_attention: 'وصلنا لحد الجولات' })[state.phase] || 'قيد العمل';
  $('round-label').textContent = 'الجولة ' + (state.round || 0) + ' من ' + (state.max_rounds || 4);
  $('calls-label').textContent = (state.usage?.calls || 0) + ' طلب API · ' + ((state.usage?.input || 0) + (state.usage?.output || 0)).toLocaleString() + ' توكن';
  $('cost-label').textContent = 'Kimi ~$' + (state.usage?.kimi_estimate_usd || 0).toFixed(3);
  $('progress-fill').style.width = (state.phase === 'complete' ? 100 : Math.min(93, (state.round || 0) / (state.max_rounds || 4) * 85 + (state.phase === 'reviewing' ? 10 : 0))) + '%';
  $('cancel-btn').disabled = !['building', 'reviewing', 'planning'].includes(state.phase);
  $('file-list').replaceChildren();
  if (!(state.files || []).length) $('file-list').append(node('span', '', 'راح تظهر الملفات هنا'));
  else (state.files || []).forEach(file => $('file-list').append(node('span', '', '▣  ' + file)));
  $('checks').textContent = state.checks || 'بانتظار أول عملية فحص...';
  renderEvents(state, $('progress-events'), 3);
  if (!historyView) {
    renderEvents(state, $('events'), 100);
    $('event-count').textContent = (state.events || []).length + ' حدث';
  }
  if (state.phase === 'error' && state.error && window.lastError !== state.id + state.error) {
    window.lastError = state.id + state.error;
    toast(state.error, true);
  }
}

async function poll() {
  const response = await fetch('/api/state', { cache: 'no-store' });
  if (!response.ok) throw new Error('الخادم غير متاح.');
  const data = await response.json();
  csrf = data.csrf;
  settings = data.settings;
  render(data.state);
  if (!window.settingsLoaded) {
    window.settingsLoaded = true;
    loadRole();
  }
}

function loadRole() {
  const profile = settings[role] || {};
  $('provider').value = profile.provider || 'auto';
  $('reasoning').value = profile.reasoning_effort || 'high';
  $('base-url').value = profile.base_url || '';
  $('model').value = profile.model || '';
  $('api-key').value = '';
  $('key-state').textContent = profile.has_key ? 'محفوظ · أدخل قيمة جديدة لتغييره' : 'لم يُحفَظ مفتاح';
  $('models').replaceChildren();
  document.querySelectorAll('.role-tab').forEach(item => item.classList.toggle('active', item.dataset.role === role));
  updateHint();
}

function draftSettings() {
  return { role, provider: $('provider').value, base_url: $('base-url').value.trim(), model: $('model').value.trim(), api_key: $('api-key').value.trim(), reasoning_effort: $('reasoning').value, apply_all: $('apply-all').checked };
}

function updateHint() {
  const value = $('provider').value;
  const hints = {
    auto: 'التعرف التلقائي يتوقف إذا كان المفتاح يشبه مفاتيح عدة مزوّدين؛ اختر المزوّد يدويًا حينها.',
    kimi: 'استخدم مفتاح منصة Kimi. الاسم الرسمي لـ K3 هو kimi-k3.',
    openai: 'يستخدم واجهة Responses الرسمية؛ اختر نموذجًا يدعم إنشاء النصوص.',
    gemini: 'اختر نموذجًا من قائمة generateContent المتاحة لحسابك.',
    anthropic: 'سيستخدم Claude Messages API. اختر النموذج المتاح لمفتاحك.',
    compatible: 'اكتب عنوان OpenAI-compatible ينتهي غالبًا بـ /v1، واسم النموذج الفعلي.',
    demo: 'مثال محلي بلا ذكاء اصطناعي ولا تكلفة؛ يساعدك تختبر الواجهة ومسار العمل.'
  };
  $('provider-hint').textContent = hints[value];
}

async function loadHistory() {
  try {
    const res = await fetch('/api/history');
    const data = await res.json();
    $('history').replaceChildren();
    if (!data.history.length) $('history').append(node('p', 'muted', 'ماكو جلسات محفوظة.'));
    for (const item of data.history) {
      const button = node('button', 'history-item');
      button.append(node('b', '', item.prompt || 'مشروع بلا اسم'), node('small', '', (item.phase || '') + ' · ' + (item.workspace || 'بدون مجلد')));
      button.addEventListener('click', async () => {
        const response = await fetch('/api/history/' + encodeURIComponent(item.id));
        const past = await response.json();
        if (!response.ok) return toast(past.error, true);
        historyView = true;
        renderEvents(past.state, $('events'), 100);
        $('event-count').textContent = (past.state.events || []).length + ' حدث · جلسة سابقة';
        toast('عرض جلسة سابقة. ارجع للاستوديو لمتابعة الجلسة الحالية.');
      });
      $('history').append(button);
    }
  } catch (_) { toast('تعذر تحميل سجل الجلسات.', true); }
}

document.querySelectorAll('.nav-item').forEach(item => item.addEventListener('click', () => selectTab(item.dataset.tab)));
document.querySelectorAll('.role-tab').forEach(item => item.addEventListener('click', () => { role = item.dataset.role; loadRole(); }));
$('provider').addEventListener('change', updateHint);
$('history-refresh').addEventListener('click', loadHistory);
$('start-btn').addEventListener('click', () => action('start', { prompt: $('prompt').value, max_rounds: Number($('max-rounds').value), max_calls: Number($('max-calls').value) }, 'بدأ المنسّق بمراجعة فكرتك.'));
$('answer-btn').addEventListener('click', () => {
  const answers = [...$('questions-list').querySelectorAll('textarea')].map(item => item.value.trim());
  action('answer', { answers }, 'وصلت الإجابات إلى المنسّق.');
});
$('approve-btn').addEventListener('click', () => action('approve', { brief: $('brief').value, acceptance: $('acceptance').value.split('\n').map(x => x.trim()).filter(Boolean), workspace: $('workspace').value.trim(), test_command: $('test-command').value.trim() }, 'بدأت دورة البناء والمراجعة.'));
$('cancel-btn').addEventListener('click', () => action('cancel', {}, 'سيتم إيقاف العمل بعد انتهاء الطلب الحالي.'));
$('models-btn').addEventListener('click', async () => {
  const button = $('models-btn');
  button.disabled = true;
  button.textContent = 'جاري الاتصال...';
  try {
    const data = await api('models', draftSettings());
    $('models').replaceChildren();
    data.models.forEach(name => { const opt = node('option'); opt.value = name; $('models').append(opt); });
    if (data.models.length === 1 && !$('model').value) $('model').value = data.models[0];
    if (data.models.includes('kimi-k3') && !$('model').value) $('model').value = 'kimi-k3';
    toast('تعرّفنا على ' + data.provider + ' · ' + data.models.length + ' نموذج متاح. اختَر اسمًا من الحقل.');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = 'جلب النماذج'; }
});
$('save-btn').addEventListener('click', async () => {
  try {
    const data = await api('settings', draftSettings());
    settings = data.settings;
    loadRole();
    toast($('apply-all').checked ? 'حُفظت الإعدادات للأدوار الثلاثة.' : 'حُفظت إعدادات هذا الدور.');
  } catch (error) { toast(error.message, true); }
});
$('toggle-key').addEventListener('click', () => { $('api-key').type = $('api-key').type === 'password' ? 'text' : 'password'; });
$('clear-key').addEventListener('click', async () => {
  if (!confirm('حذف المفتاح المحفوظ لهذا الدور؟')) return;
  try { const data = await api('settings', { ...draftSettings(), api_key: '', clear_key: true, apply_all: false }); settings = data.settings; loadRole(); toast('حُذف المفتاح من الإعدادات.'); }
  catch (error) { toast(error.message, true); }
});
poll().catch(() => toast('تعذر الاتصال بالخادم المحلي.', true));
setInterval(() => poll().catch(() => {}), 1800);
