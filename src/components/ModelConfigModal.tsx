import { useEffect, useState } from 'react';
import { api } from '../api';
import { useGraphStore } from '../store/useGraphStore';

interface ProviderConfig {
  provider: string;
  label: string;
  baseUrl: string;
  apiKey: string;
  models: string[];
}

export default function ModelConfigModal() {
  const open = useGraphStore((s) => s.settingsOpen);
  const setOpen = useGraphStore((s) => s.setSettingsOpen);
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [saving, setSaving] = useState(false);
  const [addNew, setAddNew] = useState(false);
  const [newProvider, setNewProvider] = useState<ProviderConfig>({ provider: '', label: '', baseUrl: '', apiKey: '', models: [''] });
  const [skillDir, setSkillDir] = useState('');
  const [skills, setSkills] = useState<{ name: string; description: string; path: string }[]>([]);
  const [savingSkills, setSavingSkills] = useState(false);
  const [tavilyKey, setTavilyKey] = useState('');
  const [savingTavily, setSavingTavily] = useState(false);
  const [mcpServers, setMcpServers] = useState<{ id: number; name: string; command: string; args: string[]; enabled: boolean }[]>([]);
  const [mcpName, setMcpName] = useState('');
  const [mcpCommand, setMcpCommand] = useState('');
  const [mcpArgs, setMcpArgs] = useState('');

  const loadSkills = () => {
    api.getSkills().then((r) => {
      setSkillDir(r.skillDir);
      setSkills(r.skills || []);
    }).catch(() => {});
  };

  const saveSkills = async () => {
    setSavingSkills(true);
    try {
      const r = await api.saveSkills(skillDir.trim());
      setSkillDir(r.skillDir);
      setSkills(r.skills || []);
    } catch (e) {
      console.error('保存 skill 目录失败', e);
      alert('保存 skill 目录失败，请检查路径');
    } finally {
      setSavingSkills(false);
    }
  };

  const loadTavily = () => {
    api.getTavilyConfig().then((r) => setTavilyKey(r.apiKey || '')).catch(() => {});
  };

  const saveTavily = async () => {
    setSavingTavily(true);
    try {
      const r = await api.saveTavilyConfig(tavilyKey.trim());
      setTavilyKey(r.apiKey || '');
    } catch (e) {
      console.error('保存 Tavily key 失败', e);
      alert('保存 Tavily key 失败');
    } finally {
      setSavingTavily(false);
    }
  };

  const loadMcp = () => {
    api.listMcpServers().then(setMcpServers).catch(() => {});
  };

  const addMcp = async () => {
    const name = mcpName.trim();
    const command = mcpCommand.trim();
    if (!name || !command) { alert('请填写 server 名称和启动命令'); return; }
    const args = mcpArgs.trim() ? mcpArgs.trim().split(/\s+/) : [];
    try {
      await api.addMcpServer({ name, command, args });
      setMcpName(''); setMcpCommand(''); setMcpArgs('');
      loadMcp();
    } catch (e) {
      console.error('添加 MCP server 失败', e);
      alert('添加失败，请检查命令');
    }
  };

  const delMcp = async (id: number) => {
    await api.deleteMcpServer(id).catch(() => {});
    loadMcp();
  };

  const toggleMcp = async (id: number) => {
    await api.toggleMcpServer(id).catch(() => {});
    loadMcp();
  };

  useEffect(() => {
    loadSkills();
    loadTavily();
    loadMcp();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    api.getModelConfig().then((cfg) => {
      setProviders(
        cfg.providers.map((p) => ({
          provider: p.provider,
          label: p.label,
          baseUrl: p.baseUrl,
          apiKey: p.apiKey,
          models: p.models || [],
        })),
      );
      if (!cfg.hasConfig) setOpen(true);
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (open) {
      api.getModelConfig().then((cfg) => {
        setProviders(
          cfg.providers.map((p) => ({
            provider: p.provider,
            label: p.label,
            baseUrl: p.baseUrl,
            apiKey: p.apiKey,
            models: p.models || [],
          })),
        );
      }).catch(() => {});
    }
  }, [open]);

  const setField = (key: string, field: keyof ProviderConfig, value: string) => {
    setProviders((list) =>
      list.map((p) => (p.provider === key ? { ...p, [field]: value } : p)),
    );
  };

  const save = async () => {
    setSaving(true);
    try {
      await api.saveModelProviders(
        providers.map((p) => ({ provider: p.provider, apiKey: p.apiKey, baseUrl: p.baseUrl, models: p.models })),
      );
      setOpen(false);
      window.location.reload();
    } catch (e) {
      console.error('保存失败', e);
      alert('保存失败，请检查配置');
    } finally {
      setSaving(false);
    }
  };

  const hasAnyKey = providers.some((p) => p.apiKey.trim());

  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={() => setOpen(false)}>
      <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="modal-scroll">
        <div className="modal-title">设置</div>
        <div className="modal-sub">为各服务商填入 API Key（可填多个），卡片内可选用任一已配置模型。</div>

        <div className="provider-list">
          {providers.map((p) => (
            <div key={p.provider} className="provider-row">
              <span className="provider-label">{p.label}</span>
              <input
                type="password"
                value={p.apiKey}
                placeholder="API Key（多把 key 用逗号/分号分隔）"
                onChange={(e) => setField(p.provider, 'apiKey', e.target.value)}
              />
            </div>
          ))}
        </div>

        {addNew ? (
          <div className="provider-new">
            <input placeholder="厂商标识(如 openai)" value={newProvider.provider}
              onChange={(e) => setNewProvider({ ...newProvider, provider: e.target.value })} />
            <input placeholder="显示名(如 OpenAI)" value={newProvider.label}
              onChange={(e) => setNewProvider({ ...newProvider, label: e.target.value })} />
            <input placeholder="Base URL(如 https://api.openai.com/v1)" value={newProvider.baseUrl}
              onChange={(e) => setNewProvider({ ...newProvider, baseUrl: e.target.value })} />
            <input placeholder="模型名(逗号分隔)" value={newProvider.models.join(',')}
              onChange={(e) => setNewProvider({ ...newProvider, models: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} />
            <input type="password" placeholder="API Key" value={newProvider.apiKey}
              onChange={(e) => setNewProvider({ ...newProvider, apiKey: e.target.value })} />
            <div className="modal-actions">
              <button className="btn" disabled={!newProvider.provider || !newProvider.baseUrl || !newProvider.apiKey}
                onClick={() => { setProviders([...providers, newProvider]); setAddNew(false); setNewProvider({ provider: '', label: '', baseUrl: '', apiKey: '', models: [''] }); }}>
                添加
              </button>
              <button className="btn" onClick={() => setAddNew(false)}>取消</button>
            </div>
          </div>
        ) : (
          <button className="btn" onClick={() => setAddNew(true)}>＋ 新增厂商</button>
        )}

        <div className="settings-section">
          <div className="settings-section-title">Skill（能力扩展）</div>
          <div className="settings-section-hint">指定一个 skill 目录路径，后端自动扫描加载其中的 <code>SKILL.md</code> / <code>*.md</code>，注入到每个卡片的 agent。</div>
          <div className="skill-dir-row">
            <input
              value={skillDir}
              placeholder="skill 目录路径（如 D:\skills）"
              onChange={(e) => setSkillDir(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') saveSkills(); }}
            />
            <button className="btn" disabled={savingSkills} onClick={saveSkills}>
              {savingSkills ? '加载中…' : '加载'}
            </button>
          </div>
          {skills.length > 0 ? (
            <div className="skill-list">
              {skills.map((s) => (
                <div key={s.name} className="skill-item">
                  <span className="skill-name">{s.name}</span>
                  <span className="skill-desc">{s.description}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="settings-section-hint" style={{ marginTop: 4 }}>尚未加载任何 skill。</div>
          )}
        </div>

        <div className="settings-section">
          <div className="settings-section-title">联网搜索（Tavily）</div>
          <div className="settings-section-hint">填写 Tavily API Key 后，联网搜索优先用 Tavily（专为 AI 设计，带答案摘要与可抓取的 URL），解决免费搜索反爬/不精准问题。</div>
          <div className="skill-dir-row">
            <input
              type="password"
              value={tavilyKey}
              placeholder="Tavily API Key（留空则用免费搜索）"
              onChange={(e) => setTavilyKey(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') saveTavily(); }}
            />
            <button className="btn" disabled={savingTavily} onClick={saveTavily}>
              {savingTavily ? '保存中…' : '保存'}
            </button>
          </div>
        </div>

        <div className="settings-section">
          <div className="settings-section-title">MCP Server（第三方工具接入）</div>
          <div className="settings-section-hint">添加 stdio 模式的 MCP server（如 filesystem / github），其工具会自动接入每个卡片的 agent。</div>
          <div className="mcp-add-row">
            <input placeholder="名称（如 filesystem）" value={mcpName} onChange={(e) => setMcpName(e.target.value)} />
            <input placeholder="启动命令（如 npx）" value={mcpCommand} onChange={(e) => setMcpCommand(e.target.value)} />
            <input placeholder="参数（如 -y @modelcontextprotocol/server-filesystem D:\work）" value={mcpArgs} onChange={(e) => setMcpArgs(e.target.value)} />
            <button className="btn" onClick={addMcp}>添加</button>
          </div>
          {mcpServers.length > 0 ? (
            <div className="mcp-list">
              {mcpServers.map((s) => (
                <div key={s.id} className="mcp-item">
                  <span className="mcp-item-name">{s.name}</span>
                  <span className="mcp-item-cmd">{s.command} {s.args.join(' ')}</span>
                  <button className="btn" onClick={() => toggleMcp(s.id)}>{s.enabled ? '停用' : '启用'}</button>
                  <button className="btn" onClick={() => delMcp(s.id)}>删除</button>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        <div className="modal-actions">
          <button className="btn" onClick={() => setOpen(false)}>关闭</button>
          <button className="btn btn-primary" disabled={saving || !hasAnyKey} onClick={save}>
            {saving ? '保存中…' : '保存并启用'}
          </button>
        </div>
        </div>
      </div>
    </div>
  );
}
