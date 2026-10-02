import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Empty, Space, Spin } from 'antd';
import {
  FullscreenExitOutlined,
  FullscreenOutlined,
  ReloadOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { useNavigate, useSearchParams } from 'react-router-dom';
import apiClient, { setStoredBiToken } from '../../api/client';
import { ScreenPanel } from '../../components/screen/ScreenDecor';
import {
  type BiMetricDef,
  DimFilterBar,
  MetricContent,
  type DimFilterSelection,
  resolveCells,
  useMetricsData,
  useScreenSettings,
} from './BiBoard';

/**
 * 投放页画布：看板级基准分辨率（base_w×base_h，缺省 1920×1080）+ 适配模式
 * （DataEase screenAdaptor 同名语义）+ 主题底色。
 * 画布 = 72 列 × cellW(=base_w/72) × 36 行 × rowH(=base_h/36)，
 * 与管理页同一格位坐标（同一组件 JSON，画布双解释）。
 */
const CELL_COLS = 72;
const CELL_ROWS = 36;

/** BI 独立投放模式（/bi/screen?board=key）：多看板自由画布投放 */
const BiScreenPage: React.FC = () => {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const board = searchParams.get('board') || 'default';
  const rootRef = useRef<HTMLDivElement>(null);

  // 访问令牌注入（add-bi-access-scope）：`?token=bit_…` 首次进来自动写入
  // localStorage 并**抹掉地址栏参数**（避免历史/截图/转发带出令牌）；之后走 localStorage
  useEffect(() => {
    const urlToken = searchParams.get('token');
    if (!urlToken) return;
    setStoredBiToken(urlToken);
    const url = new URL(window.location.href);
    url.searchParams.delete('token');
    window.history.replaceState({}, '', url.toString());
  }, [searchParams]);

  const [defs, setDefs] = useState<BiMetricDef[]>([]);
  const [refreshKey, setRefreshKey] = useState(0);
  const [viewport, setViewport] = useState({
    w: window.innerWidth, h: window.innerHeight,
  });
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [dimFilters, setDimFilters] = useState<DimFilterSelection>({});
  // 内容区实测尺寸：72×36 格位映射到内容区（标题栏/筛选条之下），
  // 而非整张画布——否则摆到画布底部的卡会落在内容区外被裁
  const areaRef = useRef<HTMLDivElement>(null);
  const [area, setArea] = useState({ w: 0, h: 0 });
  // 投放页每 30s 重读看板设置：管理页改适配模式/主题/筛选后大屏自动跟随
  const { settings } = useScreenSettings(board, 30_000);
  const boardTitle = settings.name
    || (board === 'default' ? '业务数据大屏' : board);
  const filterDims = settings.filter_dims ?? [];
  const cells = resolveCells(defs, settings.layout, settings.cells);

  // 基准分辨率与格位尺寸（随看板设置；内容区实测后 72×36 映射到内容区）
  const baseW = settings.base_w ?? 1920;
  const baseH = settings.base_h ?? 1080;
  const adaptor = settings.screen_adaptor ?? 'keep';
  const isLight = settings.theme === 'light';
  const cellW = area.w > 0 ? area.w / CELL_COLS : baseW / CELL_COLS;
  const rowH = area.h > 0 ? area.h / CELL_ROWS : baseH / CELL_ROWS;

  // 内容区尺寸跟踪（标题/筛选条变化或缩放后重算格位）
  useEffect(() => {
    const el = areaRef.current;
    if (!el) return undefined;
    const update = () => setArea({ w: el.clientWidth, h: el.clientHeight });
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [filterDims.length, baseW, baseH]);

  // 指标定义（只取启用项）
  useEffect(() => {
    apiClient.get('/bi/metrics', { params: { enabled: true } })
      .then((res) => setDefs(res.data ?? []))
      .catch(() => setDefs([]));
  }, []);

  // 数据刷新：1 分钟一次
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') setRefreshKey((k) => k + 1);
    }, 60_000);
    // visibilitychange（审查 P3）：隐藏标签页暂停轮询、恢复时立即刷新，
    // 无人值守大屏/后台标签不再空转打全量请求
    const onVis = () => { if (document.visibilityState === 'visible') setRefreshKey((k) => k + 1); };
    document.addEventListener('visibilitychange', onVis);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', onVis); };
  }, []);

  const { loading, payloads } = useMetricsData(defs, '24h', refreshKey, dimFilters);

  // 适配模式（DataEase DePreview.resetLayout 同款口径）：
  // keep=等比缩放居中；widthFirst=宽度撑满（高度可能溢出滚动）；
  // heightFirst=高度撑满（宽度可能溢出）；full=宽高各自拉伸（不保持比例）
  const scaleW = (viewport.w * 100) / baseW;
  const scaleH = (viewport.h * 100) / baseH;
  const scale = adaptor === 'widthFirst' ? scaleW / 100
    : adaptor === 'heightFirst' ? scaleH / 100
      : adaptor === 'full' ? 1 // full 用 transform scaleX/scaleY 分别拉伸
        : Math.min(scaleW, scaleH) / 100;
  // canvas 按基准分辨率渲染后整体 transform 缩放；wrapper 提供缩放后的
  // 文档流占位（transform 不改变布局尺寸）
  const fitW = adaptor === 'full' ? viewport.w : baseW * scale;
  const fitH = adaptor === 'full' ? viewport.h : baseH * scale;
  const canvasTransform = adaptor === 'full'
    ? `scale(${viewport.w / baseW}, ${viewport.h / baseH})`
    : `scale(${scale})`;
  // keep 模式居中：水平 margin auto + 垂直按余量居中
  //（块级 margin auto 垂直方向无效，溢出时以 0 保底不裁顶部）
  const keepTop = adaptor === 'keep' ? Math.max(0, (viewport.h - fitH) / 2) : 0;
  const keepLeft = adaptor === 'keep' ? Math.max(0, (viewport.w - fitW) / 2) : 0;

  // 全屏状态同步
  useEffect(() => {
    const onChange = () => setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else {
      void rootRef.current?.requestFullscreen();
    }
  }, []);

  // Esc 退出投放（弹层打开时让给弹层：Modal/下拉/抽屉先用 Esc 关闭，不整页退出）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      const overlayOpen = document.querySelector(
        '.ant-modal-wrap:not([style*="display: none"]), '
        + '.ant-select-dropdown:not(.ant-select-dropdown-hidden), '
        + '.ant-drawer-open, .ant-popover:not(.ant-popover-hidden)');
      const el = e.target as HTMLElement | null;
      if (overlayOpen || el?.closest('.ant-modal, .ant-select, .ant-drawer, .ant-popover')) {
        return;
      }
      navigate('/bi');
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navigate]);

  // 视口监听放在 scale 计算依赖之后（resize 时重算）
  useEffect(() => {
    const fit = () => setViewport({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, []);

  const placed = defs.filter((d) => cells[d.metric_key]);
  const dark = !isLight;

  return (
    <div
      ref={rootRef}
      className={`scr-root${isLight ? ' scr-root--light' : ''}`}
      style={{ width: '100vw', height: '100vh', overflow: 'auto' }}
    >
      {/* 占位 wrapper：文档流尺寸 = 缩放后画布；keep 模式居中（含垂直） */}
      <div style={{
        width: fitW, height: fitH,
        marginLeft: keepLeft, marginTop: keepTop,
        overflow: 'hidden',
      }}>
        {/* 画布：按基准分辨率排布后整体缩放 */}
        <div style={{
          width: baseW,
          height: baseH,
          transform: canvasTransform,
          transformOrigin: 'top left',
          position: 'relative',
        }}>
        <div style={{
          width: baseW, height: baseH,
          padding: `${baseH * 0.013}px ${baseW * 0.0125}px ${baseH * 0.018}px`,
          boxSizing: 'border-box', display: 'flex', flexDirection: 'column',
        }}>
          {/* 标题栏 */}
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            marginBottom: baseH * 0.011,
          }}>
            <div style={{ fontSize: Math.round(baseH * 0.024), fontWeight: 700, letterSpacing: 2 }}>
              {boardTitle}
            </div>
            <Space size={12}>
              <span style={{ fontSize: 13, opacity: 0.7 }}>
                {new Date().toLocaleString('zh-CN')}
              </span>
            </Space>
          </div>

          {/* 筛选元素（看板设置中启用；选中值联动多图） */}
          {filterDims.length > 0 && (
            <DimFilterBar defs={defs} dimNames={filterDims} dark={dark}
                          value={dimFilters} onChange={setDimFilters} />
          )}

          {/* 自由画布内容区：绝对定位，格位坐标 × 内容区实测 cell 尺寸 */}
          <div ref={areaRef} style={{ flex: 1, minHeight: 0, position: 'relative' }}>
            {placed.length === 0 ? (
              <Empty description="本看板还没有摆放卡片（在管理页布局编辑中添加）"
                     style={{ marginTop: 120 }} />
            ) : (
              <Spin spinning={loading}>
                {placed.map((def) => {
                  const c = cells[def.metric_key];
                  const w = c.w * cellW - 8;
                  const h = c.h * rowH - 8;
                  return (
                    <div key={def.metric_key} style={{
                      position: 'absolute',
                      left: c.x * cellW,
                      top: c.y * rowH,
                      width: w,
                      height: h,
                    }}>
                      <ScreenPanel title={def.name} extra={def.granularity}
                                   height={h} light={isLight}>
                        <MetricContent def={def} payload={payloads[def.metric_key]}
                                       loading={loading} dark={dark}
                                       autoScroll
                                       height={Math.max(100, h - 40)} />
                      </ScreenPanel>
                    </div>
                  );
                })}
              </Spin>
            )}
          </div>
        </div>
        </div>
      </div>

      {/* 悬浮工具条（鼠标悬停才显现，避免干扰投放画面） */}
      <div className="scr-bar">
        <Button size="small" icon={<ReloadOutlined />}
                onClick={() => setRefreshKey((k) => k + 1)}>刷新</Button>
        <Button size="small" icon={<SettingOutlined />}
                onClick={() => navigate('/bi/definitions')}>指标定义</Button>
        <Button size="small" icon={isFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
                onClick={toggleFullscreen}>
          {isFullscreen ? '退出全屏' : '全屏'}
        </Button>
        <Button size="small" onClick={() => navigate('/bi')}>返回管理</Button>
      </div>
      <div className="scr-footer" />
    </div>
  );
};

export default BiScreenPage;
