(() => {
  let channels = [];
  const field = name => document.querySelector(`#image-channel-${name}`);
  const form = field('form');
  function reset() {
    form.reset(); field('id').value = ''; field('key').required = true;
    field('key').placeholder = '独立配置生图权限 Key';
    field('save').textContent = '新增图片渠道';
  }
  async function load() {
    try {
      channels = await api('/image-channels');
      field('grid').innerHTML = channels.map(row => `<article class="channel-card ${row.is_active ? 'active' : ''}">
        <h3>${escapeHtml(row.name)}</h3><p>${escapeHtml(row.model_name)} · ${row.is_active ? '已启用' : '未启用'}</p>
        <p>${escapeHtml(row.base_url)}</p><p>QPS ${row.qps_limit} · 密钥${row.key_configured ? '已配置' : '未配置'}</p>
        <button class="small-btn" type="button" data-image-edit="${row.id}">编辑 / 启停</button></article>`).join('')
        || '<p>尚未配置图片渠道；可上传图片使用白板产线。</p>';
    } catch (error) { notify(error.message); }
  }
  document.querySelector('[data-page="image-channels"]').addEventListener('click', () => {
    document.querySelector('#page-name').textContent = '图片生成渠道'; load();
  });
  field('cancel').addEventListener('click', reset);
  field('grid').addEventListener('click', event => {
    const button = event.target.closest('[data-image-edit]');
    if (!button) return;
    const row = channels.find(item => item.id === button.dataset.imageEdit);
    for (const [name, key] of [['id', 'id'], ['name', 'name'], ['url', 'base_url'], ['model', 'model_name'], ['qps', 'qps_limit']]) field(name).value = row[key];
    field('key').value = ''; field('key').required = false; field('key').placeholder = '留空保留原 Key';
    field('active').checked = row.is_active; field('save').textContent = '保存修改';
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const id = field('id').value;
    const payload = {name: field('name').value, base_url: field('url').value,
      model_name: field('model').value, qps_limit: Number(field('qps').value), is_active: field('active').checked};
    if (field('key').value) payload.api_key = field('key').value;
    field('save').disabled = true;
    try {
      await api(`/image-channels${id ? '/' + id : ''}`, {method: id ? 'PUT' : 'POST', body: JSON.stringify(payload)});
      reset(); await load(); notify('图片渠道已保存；没有调用付费生图接口。');
    } catch (error) { notify(error.message); }
    finally { field('save').disabled = false; }
  });
})();
