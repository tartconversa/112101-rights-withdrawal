# 内容授权使用资料库

授权主张、内容版本关系和渠道使用的合成来源资料。

原始资料使用 UTF-8 CSV 保存，时间值采用带时区的 ISO 8601 格式。资料为虚构样例，其中包含不同事件时间、记录时间和待核实状态，不对应真实个人或平台。

## 目录

- `sources/` 保存原始业务记录。
- `sources/grants.csv`
- `sources/content_versions.csv`
- `sources/uses.csv`
- `src/rights_facts/` 是 Python 包入口。
- `tests/` 检查随项目提供的源文件格式。

## 环境

项目仅使用 Python 标准库，无需访问网络或安装第三方包。

## 运行基线检查

```powershell
python -m unittest discover -s tests -v
```
