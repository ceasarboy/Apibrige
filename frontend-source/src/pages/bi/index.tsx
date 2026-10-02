import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button, Card, Empty, Input, Modal, Popconfirm, Popover, Select, Space, Spin,
  Switch, Tabs, Tag, message,
} from 'antd';
import {
  AppstoreAddOutlined,
  DeleteOutlined,
  EditOutlined,
  ExperimentOutlined,
  FullscreenOutlined,
  SafetyOutlined,
  PlusOutlined,
  ReloadOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import GridLayout, { useContainerWidth } from 'react-grid-layout';
import type { Layout } from 'react-grid-layout';
import 'react-grid-layout/css/styles.css';
import 'react-resizable/css/styles.css';
import apiClient from '../../api/client';
import {
  CANVAS_COLS,
  CANVAS_ROW_HEIGHT,
  type BiMetricDef,
  DimFilterBar,
  MetricContent,
  nextFreeCell,
  PERIOD_OPTIONS,
  ScreenDisplaySettings,
  ScreenFilterSettings,
  resolveCells,
  useBoards,
  useMetricsData,
  useScreenSettings,
} from './BiBoard';

/** 业务数据大屏（管理界面内；数据源 = 连接表单的业务数据；多看板，摆放即归属） */
const BiPage: React.FC = () => {
  const navigate = useNavigate();
  const [boards, setBoards] = useState<{ key: string; name: string }[]>([]);
  const [board, setBoard] = useState('default');
  const [defs, setDefs] = useState<BiMetricDef[]>([]);
  const [period, setPeriod] = useState('24h');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [dimFilters, setDimFilters] = useState<Record<string, string>>({});
  const [editingLayout, setEditingLayout] = useState(false);
  const [newBoardOpen, setNewBoardOpen] = useState(false);
  const [newBoardName, setNewBoardName] = useState('');
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameName, setRenameName] = useState('');
  const { settings, save, loaded, loadError, reload: reloadSettings } = useScreenSettings(board);
  const boardOps = useBoards();
  const layout = settings.layout ?? {};
  const {
    width: gridWidth, containerRef: gridRef,
  } = useContainerWidth({ initialWidth: 1200 });
  const boardName = boards.find((b) => b.key === board)?.name
    ?? settings.name ?? (board === 'default' ? '默认看板' : board);

  const loadBoards = useCallback((keep?: string) => {
    apiClient.get('/bi/screens')
      .then((r) => {
        const list = r.data ?? [];
        setBoards(list);
        if (keep && list.some((b: { key: string }) => b.key === keep)) {
          setBoard(keep);
        }
      })
      .catch(() => setBoards([]));
  }, []);

  useEffect(() => { loadBoards(); }, [loadBoards]);

  useEffect(() => {
    apiClient.get('/bi/metrics', { params: { enabled: true } })
      .then((res) => setDefs(res.data ?? []))
      .catch(() => setDefs([]));
  }, []);

  // 1 分钟自动刷新（task 5.5）
  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') setRefreshKey((k) => k + 1);
    }, 60_000);
    // visibilitychange（审查 P3）：隐藏标签页暂停轮询、恢复时立即刷新，
    // 无人值守大屏/后台标签不再空转打全量请求
    const onVis = () => { if (document.visibilityState === 'visible') setRefreshKey((k) => k + 1); };
    document.addEventListener('visibilitychange', onVis);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', onVis); };
  }, [autoRefresh]);

  const { loading, payloads } = useMetricsData(defs, period, refreshKey, dimFilters);

  // 多看板语义「摆放即归属」：cells 里摆了的指标才上屏
  const cells = useMemo(
    () => resolveCells(defs, layout, settings.cells),
    [defs, layout, settings.cells],
  );
  const visibleDefs = useMemo(
    () => defs.filter((d) => cells[d.metric_key]),
    [defs, cells],
  );
  // 未上屏指标（布局编辑时作为可添加卡片库）
  const paletteDefs = useMemo(
    () => defs.filter((d) => !cells[d.metric_key]),
    [defs, cells],
  );

  // RGL 拖拽/缩放结束 → 逐卡格位持久化
  const handleGridSettled = (l: Layout) => {
    save({
      ...settings,
      cells: Object.fromEntries(
        l.map((i) => [i.i, { x: i.x, y: i.y, w: i.w, h: i.h }]),
      ),
    });
  };

  // 添加卡片（从卡片库放入画布最底行）；移除卡片
  const addCard = (key: string) => {
    const cell = nextFreeCell(cells);
    if (!cell) { message.info('画布没有足够空间，请先缩小或移除卡片'); return; }
    save({ ...settings, cells: { ...cells, [key]: cell } });
  };
  const removeCard = (key: string) => {
    const rest = { ...cells };
    delete rest[key];
    save({ ...settings, cells: rest });
  };

  const rglLayout = useMemo<Layout>(
    () => Object.entries(cells).map(([key, c]) => ({ i: key, ...c })),
    [cells],
  );

  const createBoard = async () => {
    const name = newBoardName.trim();
    if (!name) return;
    const key = await boardOps.create(name);
    if (key) {
      setNewBoardOpen(false);
      setNewBoardName('');
      loadBoards(key);
      setEditingLayout(true); // 新看板从空画布开始，直接进编辑模式摆卡
    }
  };

  const doRename = async () => {
    const name = renameName.trim();
    if (!name) return;
    if (await boardOps.rename(board, name)) {
      setRenameOpen(false);
      loadBoards(board);
    }
  };

  const doDeleteBoard = async () => {
    if (await boardOps.remove(board)) {
      loadBoards('default');
      setEditingLayout(false);
    }
  };

  return (
    <div>
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginBottom: 20, flexWrap: 'wrap', gap: 12,
      }}>
        <Space size={8} wrap>
          <h1 className="page-heading" style={{ marginBottom: 0 }}>业务数据大屏</h1>
          <Select
            size="small"
            value={board}
            onChange={(k) => { setBoard(k); setDimFilters({}); setEditingLayout(false); }}
            style={{ minWidth: 160 }}
            options={boards.map((b) => ({ value: b.key, label: b.name }))}
          />
          <Button size="small" icon={<PlusOutlined />}
                  onClick={() => setNewBoardOpen(true)}>新建看板</Button>
          <Button size="small" icon={<EditOutlined />}
                  disabled={false}
                  onClick={() => { setRenameName(boardName); setRenameOpen(true); }}>
            重命名
          </Button>
          <Popconfirm
            title={`删除看板「${boardName}」？`}
            description="仅删除画布摆放，指标定义不受影响"
            okButtonProps={{ danger: true }}
            onConfirm={doDeleteBoard}
          >
            <Button size="small" danger icon={<DeleteOutlined />}
                    disabled={board === 'default'}>删除</Button>
          </Popconfirm>
        </Space>
        <Space>
          <Select
            size="small"
            value={period}
            onChange={setPeriod}
            style={{ width: 150 }}
            options={PERIOD_OPTIONS}
          />
          <Space size={4}>
            <span style={{ fontSize: 12, color: 'var(--nb-text-secondary)' }}>自动刷新</span>
            <Switch size="small" checked={autoRefresh} onChange={setAutoRefresh} />
          </Space>
          <Button size="small" icon={<ReloadOutlined />}
                  onClick={() => {
                    setRefreshKey((k) => k + 1);
                    reloadSettings();   // 设置读取失败（loadError）后，页内刷新必须能重试
                  }}>刷新</Button>
          <Button size="small" type={editingLayout ? 'primary' : 'default'}
                  onClick={() => setEditingLayout((v) => !v)}>
            {editingLayout ? '退出布局编辑' : '布局编辑'}
          </Button>
          <Popover
            trigger="click" placement="bottomRight"
            content={(
              <Tabs
                size="small" style={{ minWidth: 320 }}
                items={[
                  {
                    key: 'display', label: '投放显示',
                    children: (
                      <ScreenDisplaySettings
                        settings={settings}
                        onChange={(s) => save(s)} />
                    ),
                  },
                  {
                    key: 'filters', label: '筛选元素',
                    children: (
                      <ScreenFilterSettings defs={visibleDefs}
                                           selected={settings.filter_dims ?? []}
                                           onChange={(dims) => save({ ...settings, filter_dims: dims })} />
                    ),
                  },
                ]}
              />
            )}
          >
            <Button size="small" icon={<SettingOutlined />}>看板设置</Button>
          </Popover>
          <Button size="small" icon={<SettingOutlined />}
                  onClick={() => navigate('/bi/definitions')}>指标定义</Button>
          <Button size="small" icon={<ExperimentOutlined />}
                  onClick={() => navigate('/bi/analysis')}>分析工作台</Button>
          <Button size="small" icon={<SafetyOutlined />}
                  onClick={() => navigate('/bi/access')}>访问授权</Button>
          <Button size="small" type="primary" icon={<FullscreenOutlined />}
                  onClick={() => window.open(`/bi/screen?board=${encodeURIComponent(board)}`, '_blank')}>
            投放模式
          </Button>
        </Space>
      </div>

      {defs.length === 0 ? (
        <Card>
          <Empty description="暂无启用的 BI 指标（可在「指标定义」中启用或新建）" />
        </Card>
      ) : (
        <>
          {/* 筛选元素（看板设置中启用；选中值联动多图） */}
          {(settings.filter_dims ?? []).length > 0 && (
            <DimFilterBar defs={visibleDefs} dimNames={settings.filter_dims ?? []}
                          value={dimFilters} onChange={setDimFilters} />
          )}

          {/* 布局编辑提示条 */}
          {editingLayout && (
            <div style={{
              marginBottom: 12, padding: '8px 12px', borderRadius: 6,
              background: 'var(--nb-sidebar, #F7F7F5)',
              border: '1px dashed var(--nb-primary)',
              fontSize: 12, color: 'var(--nb-text-secondary)',
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            }}>
              <span>布局编辑中：拖动摆放、拖角/边调整大小，卡片右上角 × 移出看板（自动保存）</span>
              <Button size="small" type="primary"
                      onClick={() => setEditingLayout(false)}>完成</Button>
            </div>
          )}

          {/* 卡片库（布局编辑时显示未上屏指标，点击添加；空看板加第一张卡也靠它） */}
          {editingLayout && paletteDefs.length > 0 && (
            <div style={{
              marginBottom: 12, padding: '8px 12px', borderRadius: 6,
              background: 'var(--nb-sidebar, #F7F7F5)',
            }}>
              <Space size={6} wrap>
                <span style={{ fontSize: 12, color: 'var(--nb-text-secondary)' }}>
                  <AppstoreAddOutlined /> 卡片库（点击添加到画布）：
                </span>
                {paletteDefs.map((d) => (
                  <Tag.CheckableTag key={d.metric_key} checked={false}
                                    style={{ border: '1px solid var(--nb-border, #ddd)', cursor: 'pointer' }}
                                    onChange={() => addCard(d.metric_key)}>
                    <PlusOutlined /> {d.name}
                  </Tag.CheckableTag>
                ))}
              </Space>
            </div>
          )}

          {loadError ? (
            // 读取失败 ≠ 空看板：此时禁止布局写入（save 已在 hook 内拦截），
            // 引导刷新而不是"添加卡片"（那会以空设置覆盖现有布局）
            <Card>
              <Empty description="看板设置读取失败，为避免覆盖现有布局已暂停编辑；请点击刷新重试" />
            </Card>
          ) : visibleDefs.length === 0 && !editingLayout && loaded ? (
            <Card>
              <Empty description="本看板还没有摆放卡片：点击「布局编辑」，从卡片库添加" />
            </Card>
          ) : (
          <Spin spinning={loading && !editingLayout}>
            <div ref={gridRef}>
              {gridWidth > 0 && (
                <GridLayout
                  width={gridWidth}
                  layout={rglLayout}
                  gridConfig={{
                    cols: CANVAS_COLS, rowHeight: CANVAS_ROW_HEIGHT,
                    margin: [8, 8],
                  }}
                  dragConfig={{ enabled: editingLayout }}
                  resizeConfig={{ enabled: editingLayout, handles: ['se', 'e', 's'] }}
                  onDragStop={handleGridSettled}
                  onResizeStop={handleGridSettled}
                >
                  {visibleDefs.map((def) => (
                    <div key={def.metric_key} style={{ height: '100%' }}>
                      <Card size="small" title={def.name}
                            style={{ height: '100%' }}
                            styles={{ body: { height: 'calc(100% - 40px)', overflow: 'hidden' } }}
                            extra={(
                              <Space size={6}>
                                <span style={{
                                  fontSize: 12, color: 'var(--nb-text-secondary)',
                                }}>
                                  {def.granularity}
                                </span>
                                {editingLayout && (
                                  <Popconfirm title="从本看板移除该卡片？"
                                              onConfirm={() => removeCard(def.metric_key)}>
                                    <Button type="text" size="small" danger
                                            icon={<DeleteOutlined />}
                                            style={{ height: 18, width: 18, minWidth: 18 }} />
                                  </Popconfirm>
                                )}
                              </Space>
                            )}>
                        <MetricContent def={def} payload={payloads[def.metric_key]}
                                       loading={loading} dark={false} height="100%" />
                      </Card>
                    </div>
                  ))}
                </GridLayout>
              )}
            </div>
          </Spin>
          )}
        </>
      )}

      {/* 新建看板 */}
      <Modal
        title="新建看板" open={newBoardOpen} okText="创建" cancelText="取消"
        onOk={createBoard} onCancel={() => setNewBoardOpen(false)} width={420}
      >
        <Input placeholder="看板名称（如：生产运行管理、人员技能）"
               value={newBoardName}
               onChange={(e) => setNewBoardName(e.target.value)}
               onPressEnter={createBoard} />
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--nb-text-secondary)' }}>
          创建后进入布局编辑，从卡片库把指标摆上画布；每个看板有独立的投放地址。
        </div>
      </Modal>

      {/* 重命名看板 */}
      <Modal
        title="重命名看板" open={renameOpen} okText="保存" cancelText="取消"
        onOk={doRename} onCancel={() => setRenameOpen(false)} width={420}
      >
        <Input value={renameName} onChange={(e) => setRenameName(e.target.value)}
               onPressEnter={doRename} />
      </Modal>
    </div>
  );
};

export default BiPage;
