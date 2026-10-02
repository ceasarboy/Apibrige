import React from 'react';
import './screen.css';

/**
 * 自研大屏装饰组件（task 5.2）
 *
 * 不引入任何 DataV 实现（React 生态无可靠官方版本），
 * 翻牌 / 边框 / 发光全部用 CSS 实现，零外部依赖。
 */

export type Tone = 'default' | 'success' | 'warning' | 'danger';

export interface ScreenPanelProps {
  title?: React.ReactNode;
  extra?: React.ReactNode;
  /** 固定高度；不传则由内容撑开 */
  height?: number | string;
  className?: string;
  /** 白底主题（scr-root--light 下的面板浅色化） */
  light?: boolean;
  children: React.ReactNode;
}

export const ScreenPanel: React.FC<ScreenPanelProps> = ({
  title,
  extra,
  height,
  className,
  light = false,
  children,
}) => (
  <div className={`scr-panel${light ? ' scr-panel--light' : ''}${className ? ` ${className}` : ''}`}
       style={{ height }}>
    {title ? (
      <div className="scr-panel__head">
        <span className="scr-panel__title">{title}</span>
        {extra ? <span className="scr-panel__extra">{extra}</span> : null}
      </div>
    ) : null}
    <div className="scr-panel__body">{children}</div>
  </div>
);

export interface DataCardProps {
  label: string;
  value: React.ReactNode;
  unit?: string;
  tone?: Tone;
  hint?: string;
  /** true=深色大屏卡（默认）；false=浅色管理页卡 */
  dark?: boolean;
  /** 自定义底色（KPI 样式配置；优先于 dark 默认） */
  bgColor?: string;
  /** 自定义字体颜色（标签与数值） */
  fontColor?: string;
}

/** 数字翻牌卡：大屏顶栏 KPI */
export const DataCard: React.FC<DataCardProps> = ({
  label,
  value,
  unit,
  tone = 'default',
  hint,
  dark = true,
  bgColor,
  fontColor,
}) => {
  // 浅色页面默认白底描边，避免深色卡在浅色页面上突兀；
  // 用户自定义配色（bgColor/fontColor）优先级最高
  const container: React.CSSProperties = {};
  const labelStyle: React.CSSProperties = {};
  const valueStyle: React.CSSProperties = {};
  if (!dark && !bgColor) {
    container.background = '#ffffff';
    container.border = '1px solid var(--nb-border, #e5e5e5)';
    container.boxShadow = 'var(--nb-shadow, none)';
    labelStyle.color = 'var(--nb-text-secondary, #8a8a8a)';
    valueStyle.color = 'var(--nb-text, #333)';
  }
  if (bgColor) {
    container.background = bgColor;
  }
  if (fontColor) {
    labelStyle.color = fontColor;
    valueStyle.color = fontColor;
  }
  return (
    <div className={`scr-card scr-card--${tone}`} style={container}>
      <div className="scr-card__label" style={labelStyle}>{label}</div>
      <div className="scr-card__value">
        <span className="scr-card__num" style={valueStyle}>
          {value === null || value === undefined || value === '' ? '—' : value}
        </span>
        {unit ? <span className="scr-card__unit" style={labelStyle}>{unit}</span> : null}
      </div>
      {hint ? <div className="scr-card__hint" style={labelStyle}>{hint}</div> : null}
    </div>
  );
};

/** 根据数值给出语义色调（成功率、失败数等） */
export function toneForRate(rate: number | null, warn = 95, danger = 80): Tone {
  if (rate === null || Number.isNaN(rate)) return 'default';
  if (rate < danger) return 'danger';
  if (rate < warn) return 'warning';
  return 'success';
}
