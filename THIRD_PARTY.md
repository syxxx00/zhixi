# 枝析第三方说明

## 网页后台依赖

[requirements.txt](requirements.txt) 中固定的直接依赖如下。它们由用户运行 `setup.bat` 时安装，本仓库不附带这些库的源码、安装包或虚拟环境。

| 依赖 | 版本 | 发行包声明的许可证 | 项目及授权来源 |
| --- | --- | --- | --- |
| FastAPI | 0.141.1 | MIT | [PyPI](https://pypi.org/project/fastapi/0.141.1/) · [项目](https://github.com/fastapi/fastapi) |
| Uvicorn | 0.52.4 | BSD-3-Clause | [PyPI](https://pypi.org/project/uvicorn/0.52.4/) · [项目](https://github.com/Kludex/uvicorn) |
| Pydantic | 2.13.4 | MIT | [PyPI](https://pypi.org/project/pydantic/2.13.4/) · [项目](https://github.com/pydantic/pydantic) |
| HTTPX | 0.28.1 | BSD-3-Clause | [PyPI](https://pypi.org/project/httpx/0.28.1/) · [项目](https://github.com/encode/httpx) |
| ijson | 3.5.1 | BSD-3-Clause AND ISC | [PyPI](https://pypi.org/project/ijson/3.5.1/) · [项目](https://github.com/ICRAR/ijson) |
| Pillow | 12.3.0 | MIT-CMU | [PyPI](https://pypi.org/project/pillow/12.3.0/) · [项目](https://github.com/python-pillow/Pillow) |

`requirements-ops.txt` 中的 ijson 已包含在上述主依赖中。安装工具还会解析传递依赖，各自的版权及许可见所安装发行包。本表不是整个 Python 环境的完整依赖清单。

## 模型与推理工具

本仓库不附带模型权重、Python、PyTorch、Transformers、llama.cpp 或 GPU 运行库。使用本地模型时，按所选模型与工具的许可自行取得并配置；使用模型或搜索 API 时，遵守对应服务的使用条款。

可从 [llama.cpp](https://github.com/ggml-org/llama.cpp)、[PyTorch](https://pytorch.org/)、[Transformers](https://github.com/huggingface/transformers) 的官方项目了解安装要求。兼容接口或在界面中使用某个名称，不表示本仓库包含或获得了该模型、服务的再分发授权。

## 用户材料与公开网页

导入的文本、抓取的网页、评论、图片及其他媒体仍受各自权利和访问条件约束。报告中的引用、摘要或链接不改变原材料的权利归属；转载、公开报告或另行分发前，应核对来源和适用条件。

枝析自身代码与随附文档采用 [Apache License 2.0](LICENSE)。该许可不替代第三方依赖、模型、服务与原始材料各自的授权或使用条件。
