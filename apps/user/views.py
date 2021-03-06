from django.shortcuts import render, redirect
import re
from django.urls import reverse
from user.models import User, Address
from django.views.generic import View
from itsdangerous import TimedJSONWebSignatureSerializer as Serializer
from itsdangerous import SignatureExpired
from django.conf import settings
from django.http import HttpResponse
from celery_tasks.tasks import send_register_active_email
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django_redis import get_redis_connection
from goods.models import GoodsSKU
from order.models import OrderInfo, OrderGoods
from django.core.paginator import Paginator


# /user/register
def register(request):
    """
    显示注册页面
    :param request:
    :return:
    """
    if request.method == 'GET':
        return render(request, 'register.html')
    else:
        """进行注册处理"""
        # 接受数据
        username = request.POST.get('user_name')
        password = request.POST.get('pwd')
        email = request.POST.get('email')
        allow = request.POST.get('allow')
        # 数据校验
        if not all([username, password, email]):
            # 数据不完整
            return render(request, 'register.html', {'errmsg': '数据不完整'})
        if not re.match(r'^[a-z0-9][\w.\-]*@[a-z0-9\-]+(\.[a-z]{2,5}){1,2}$', email):
            return render(request, 'register.html', {'errmsg': '邮箱不正确'})
        if allow != 'on':
            return render(request, 'register.html', {'errmsg': '请同意协议'})
        # 校验用户名是否重复
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            # 用户不存在
            user = None
        # 业务处理
        if user:
            return render(request, 'register.html', {'errmsg': '用户已存在'})
        user = User.objects.create_user(username, email, password)
        user.is_active = 0
        user.save()
        # 返回应答,跳转到首页
        return redirect(reverse('goods:index'))


class RegisterView(View):
    def get(self, request):
        return render(request, 'register.html')

    def post(self, request):
        # 接受数据
        username = request.POST.get('user_name')
        password = request.POST.get('pwd')
        email = request.POST.get('email')
        allow = request.POST.get('allow')
        # 数据校验
        if not all([username, password, email]):
            # 数据不完整
            return render(request, 'register.html', {'errmsg': '数据不完整'})
        if not re.match(r'^[a-z0-9][\w.\-]*@[a-z0-9\-]+(\.[a-z]{2,5}){1,2}$', email):
            return render(request, 'register.html', {'errmsg': '邮箱不正确'})
        if allow != 'on':
            return render(request, 'register.html', {'errmsg': '请同意协议'})
        # 校验用户名是否重复
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            # 用户不存在
            user = None
        # 业务处理
        if user:
            return render(request, 'register.html', {'errmsg': '用户已存在'})
        user = User.objects.create_user(username, email, password)
        user.is_active = 0
        user.save()

        # 发送邮件给用户
        serializer = Serializer(settings.SECRET_KEY, 3600)
        info = {'confirm': user.id}
        token = serializer.dumps(info)
        token = token.decode('utf8')

        send_register_active_email.delay(email, username, token)
        # 返回应答,跳转到首页
        return redirect(reverse('goods:index'))


class ActiveView(View):
    """用户激活"""

    def get(self, request, token):
        # 解密
        serializer = Serializer(settings.SECRET_KEY, 3600)
        try:
            info = serializer.loads(token)
            user_id = info['confirm']
            user = User.objects.get(id=user_id)
            user.is_active = 1
            user.save()
            return redirect(reverse('user:login'))
        except SignatureExpired:
            return HttpResponse("激活链接已过期")


# /user/login
class LoginView(View):
    '''登录'''

    def get(self, request):
        # 显示登录页面
        # 是否记住用户名
        if 'username' in request.COOKIES:
            username = request.COOKIES.get('username')
            checked = 'checked'
        else:
            username = ''
            checked = ''
        return render(request, 'login.html', {'username': username, 'checked': checked})

    def post(self, request):
        # 接受数据
        username = request.POST.get('username')
        password = request.POST.get('pwd')
        # 校验数据
        if not all([username, password]):
            return render(request, 'login.html', {'errmsg': '数据不完整'})
        # 业务处理
        user = authenticate(username=username, password=password)
        if user is not None:
            if user.is_active:
                # 记录用户登录状态
                login(request, user)
                # 获取登录后所要跳转的地址,默认跳转到首页
                next_url = request.GET.get('next', reverse('goods:index'))
                # 跳转到next_url
                response = redirect(next_url)  # httpResponseRedirect
                remember = request.POST.get('remember')
                if remember == 'on':
                    response.set_cookie('username', username, max_age=3600 * 7 * 24)
                else:
                    response.delete_cookie('username')
                return response
            else:
                return render(request, 'login.html', {'errmsg': '用户未激活'})
        else:
            return render(request, 'login.html', {'errmsg': '用户名或密码错误'})


# /user/logout
class LogoutView(View):
    """退出登录"""
    def get(self, request):
        # 清楚用户的session信息
        logout(request)

        # 跳转到首页
        return redirect(reverse('goods:index'))


# /user/
class UserInfoView(LoginRequiredMixin, View):
    """
    用户中心页面-信息
    """
    def get(self, request):
        '''显示'''
        # 如果用户未登录->AnonymousUser的一个
        # 如果用户登录request.user是一个User对象
        # request.user.is_authenticated()

        # 获取用户的个人信息
        user = request.user
        address = Address.objects.get_default_address(user)
        # 获取用户的历史浏览记录
        con = get_redis_connection('default')

        history_key = 'history_%d' % user.id
        # 获取用户最新浏览的5条商品
        sku_ids = con.lrange(history_key, 0, 4)
        goods_li = []
        for id in sku_ids:
            id = id.decode('utf-8')
            goods = GoodsSKU.objects.filter(id=id)
            goods_li.extend(goods)
        # 组织上下文
        context = {'page': 'user', 'address': address, 'goods_li': goods_li}

        # 除了你给模板文件传递变量之外,会把request.user传给模板文件
        return render(request, 'user_center_info.html', context)


# /user/order
class UserOrderView(LoginRequiredMixin, View):
    """
    用户中心页面-订单
    """

    # 获取用户订单信息
    # @method_decorator(login_required(login_url='/user/login/', redirect_field_name='next'))
    def get(self, request, page):
        '''显示'''
        user = request.user
        orders = OrderInfo.objects.filter(user=user).order_by('-create_time')
        # 遍历获取订单商品信息
        for order in orders:
            # 根据订单信息查出购买的商品信息
            order_skus = OrderGoods.objects.filter(order_id=order.order_id)
            # 便利order_sku计算商品小计
            for order_sku in order_skus:
                # 计算小计
                amount = order_sku.count * order_sku.price
                # 动态给order_sku增加属性,保存订单商品小计
                order_sku.amount = amount
            # 动态给order生成状态
            order.status_name = OrderInfo.ORDER_STATUS[order.order_status]
            # 动态给order增加属性,保存订单商品信息
            order.order_skus = order_skus
            # 分页
            paginator = Paginator(orders, 1)
            # 处理页面
            try:
                page = int(page)
            except Exception as e:
                page = 1
            if page > paginator.num_pages:
                page = 1
            # 获取分页
            order_page = paginator.page(page)
            # 页码控制,小于5显示所有，大于五显示全部
            num_pages = paginator.num_pages
            if num_pages < 5:
                pages = range(1, num_pages + 1)
            elif page <= 3:
                pages = range(1, 6)
            elif num_pages - page <= 2:
                pages = range(num_pages - 4, num_pages + 1)
            else:
                pages = range(page - 2, page + 3)

            # 组织上下午
            context = {'order_page': order_page,
                       'pages': pages,
                       'page': 'order'
                       }
        return render(request, 'user_center_order.html', context)


# /user/address
class AddressView(LoginRequiredMixin, View):
    """
    用户中心页面-地址
    """

    # 获取用户默认收货地址
    #
    def get(self, request):
        '''显示'''

        # 获取用户登录对象
        user = request.user
        # try:
        #     address = Address.objects.get(user=user, is_default=True)
        # except Address.DoesNotExist:
        #     # 不存在收货地址
        #     address = None
        address = Address.objects.get_default_address(user)

        return render(request, 'user_center_site.html', {'page': 'address', 'address': address})

    def post(self, request):
        '''地址添加'''
        # 接受数据
        receiver = request.POST.get('receiver')
        addr = request.POST.get('addr')
        zip_code = request.POST.get('zip_code')
        phone = request.POST.get('phone')
        # 校验数据
        if not all([receiver, addr, phone]):
            return render(request, 'user_center_site.html', {'errmsg': '数据不完整'})
        if not re.match(r'^1[34578]\d{9}$', phone):
            return render(request, 'user_center_site.html', {'errmsg': '手机格式不正确'})
        # 添加地址
        # 如果用户没有收货地址,则作为收货地址，如果用户有收货地址则不做为默认收货地址

        # 获取用户登录对象
        user = request.user

        address = Address.objects.get_default_address(user)

        if address:
            is_default = False
        else:
            is_default = True

        # 添加地址
        Address.objects.create(user=user,
                               receiver=receiver,
                               addr=addr,
                               zip_code=zip_code,
                               phone=phone,
                               is_default=is_default)

        # 返回答应
        return redirect(reverse('user:address'))  # get请求
