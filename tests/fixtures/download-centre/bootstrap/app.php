<?php

use App\Http\Middleware\SetTenant;
use Illuminate\Foundation\Application;
use Illuminate\Foundation\Configuration\Middleware;

return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(api: __DIR__ . '/../routes/api.php', commands: __DIR__ . '/../routes/console.php')
    ->withMiddleware(function (Middleware $middleware) {
        $middleware->alias(['tenant' => SetTenant::class]);
    })
    ->create();
