#!/bin/bash
# 快速测试脚本（从项目根目录运行：./tools/test.sh）

# 定位项目根目录（脚本所在目录的上一级）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

echo "=========================================="
echo "Echotik Collector 优化验证"
echo "=========================================="
echo ""

# 检查环境
echo "1. 检查 Python 环境..."
if [ -f ".env" ]; then
    echo "   ✅ .env 文件存在"
else
    echo "   ❌ .env 文件不存在，请先配置"
    exit 1
fi

# 检查依赖
echo ""
echo "2. 检查依赖..."
python -c "import playwright" 2>/dev/null && echo "   ✅ playwright 已安装" || echo "   ❌ playwright 未安装"
python -c "import yaml" 2>/dev/null && echo "   ✅ yaml 已安装" || echo "   ❌ yaml 未安装"
python -c "import dotenv" 2>/dev/null && echo "   ✅ python-dotenv 已安装" || echo "   ❌ python-dotenv 未安装"

# 检查代理
echo ""
echo "3. 检查代理设置..."
if [ -n "$https_proxy" ]; then
    echo "   ✅ 代理已设置: $https_proxy"
else
    echo "   ⚠️  未检测到代理环境变量（脚本会自动设置）"
fi

echo ""
echo "=========================================="
echo "测试选项："
echo "=========================================="
echo "1. 运行页面调试工具（查看页面内容）"
echo "2. 运行导航测试（测试点击功能）"
echo "3. 运行完整测试（dry-run模式）"
echo "4. 运行真实采集（仅日榜）"
echo "5. 退出"
echo ""
read -p "请选择 (1-5): " choice

case $choice in
    1)
        echo ""
        echo "启动页面调试工具..."
        python tools/debug_page.py
        ;;
    2)
        echo ""
        echo "启动导航测试..."
        python tools/test_navigation.py
        ;;
    3)
        echo ""
        echo "运行 dry-run 测试..."
        python main.py --dry-run
        ;;
    4)
        echo ""
        echo "运行真实采集（仅日榜）..."
        python main.py --wins d
        ;;
    5)
        echo "退出"
        exit 0
        ;;
    *)
        echo "无效选择"
        exit 1
        ;;
esac
