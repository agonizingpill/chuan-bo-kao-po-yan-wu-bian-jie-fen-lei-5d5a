# 初始 schema

`src.models.Base.metadata.create_all()` 按外键顺序建表，由 `src.main` 在 API 首次启动时执行。已有开发卷不执行自动字段迁移。项目 README 记录了模型与首次建库入口。

## 间隔带校准字段（2026-11）

新增 `band_policy`、`band_calibrations`、`calibration_samples` 三张表，并在 `delay_incidents` 增加 `band`、`actual_priority`、`labelled_by`、`labelled_at`。`create_all()` 只会建缺失的表，不会给已有表补列。已有开发卷需先重建数据库或手工补列：

```sql
ALTER TABLE delay_incidents
  ADD COLUMN band FLOAT NOT NULL DEFAULT 0.30,
  ADD COLUMN actual_priority BOOL NULL,
  ADD COLUMN labelled_by VARCHAR(32) NULL,
  ADD COLUMN labelled_at DATETIME NULL;
```

`band_policy` 的初始行由启动时 `ensure_policy()` 写入，默认带宽 0.30。
