#!/bin/sh
# 删掉旧版本的沙箱镜像，只保留当前在用的那一个，然后清悬空层。
#
# 为什么不能用 `docker image prune -a -f`：
#   -a 删掉所有"当前没有容器在用"的镜像。沙箱容器是一次性的（跑完就 rm），
#   所以**绝大多数时刻当前镜像也没有容器在用** —— prune -a 会把它一起删掉。
#   而 executor 的 docker run 带的是 --pull never（刻意如此：缺镜像时不要把拉取
#   进度写进 stderr 污染 err_lines），于是下一次执行直接失败，用户只看到
#   "代码执行失败"，完全看不出是每周的清理脚本干的。
#
# 为什么不能用 `docker image prune -f`（不加 -a）：
#   那只删 dangling（无 tag）镜像。旧版本是 blog-sandbox:<旧tag>，**有 tag**，
#   删不掉 —— 每个约 1.2GB，40GB 的盘放三十个就满了，而且是静默地满。
#
# 所以只能显式按 repository 枚举、逐个与在用 tag 比对。

set -u

ENV_FILE=${SANDBOX_ENV_FILE:-/etc/sandbox-executor/executor.env}
REPO=blog-sandbox

if [ ! -r "$ENV_FILE" ]; then
    echo "读不到 $ENV_FILE，拒绝清理：不知道哪个镜像在用" >&2
    exit 1
fi

# 用 sed 取值而不是 `. "$ENV_FILE"`：source 会**执行**整个文件，那样脚本的正确性
# 就依赖"配置文件里永远只有 KEY=value"这个约定。sed 不依赖它。
# 镜像 tag 里不含空白字符，所以最后 tr -d 空白是安全的（顺带去掉引号内的空格风险）。
CURRENT=$(sed -n 's/^SANDBOX_IMAGE=//p' "$ENV_FILE" | tail -n1 | tr -d '\r' | tr -d '[:space:]')

if [ -z "$CURRENT" ]; then
    echo "$ENV_FILE 里没有 SANDBOX_IMAGE，拒绝清理" >&2
    exit 1
fi

echo "在用镜像: $CURRENT"

# 管道右侧的 while 在子 shell 里跑，所以这里不累积任何需要传出来的变量。
docker images --format '{{.Repository}}:{{.Tag}}' --filter "reference=$REPO" |
    while read -r img; do
        [ -n "$img" ] || continue
        # 一道独立于 docker 过滤器的守卫。上面的 --filter reference= 理论上只会返回
        # 本仓库的镜像，但"理论上"不够：registry 前缀形式（registry.x/blog-sandbox）
        # 会被 glob 匹配上，将来改 REPO 变量也可能改出意外。删错镜像的代价是 B 上
        # 别的服务（按计划将来可能有监控、备份容器）静默起不来，所以这里再钉一次：
        # 不以 "$REPO:" 开头的一律不碰。
        case "$img" in
            "$REPO:"*) ;;
            *) echo "  skip  $img（不属于 $REPO 仓库，过滤器却返回了它）"; continue ;;
        esac
        if [ "$img" = "$CURRENT" ]; then
            echo "  keep  $img"
        elif [ "$img" = "$REPO:<none>" ]; then
            echo "  skip  $img（悬空，交给下面的 image prune）"
        else
            echo "  rmi   $img"
            # 刻意不加 -f：有已退出的容器还引用它时应该**失败**而不是硬删 ——
            # 失败信息本身就是"为什么这个旧版本还在"的线索。
            docker rmi "$img" || echo "    删除失败（可能有容器引用），跳过"
        fi
    done

# 上面删掉 tag 之后会留下悬空层。这一步只碰 dangling，不会伤到在用的镜像。
docker image prune -f
