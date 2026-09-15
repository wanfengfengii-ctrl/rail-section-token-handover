# 多阶段构建：node 构建静态资源，nginx 承载并反代 /api。
FROM node:20-alpine AS build

WORKDIR /app

# 优先拷贝依赖清单，利用构建缓存。
COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
