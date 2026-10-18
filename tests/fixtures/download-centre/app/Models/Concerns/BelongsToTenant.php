<?php

namespace App\Models\Concerns;

use Illuminate\Database\Eloquent\Builder;

trait BelongsToTenant
{
    public static function bootBelongsToTenant(): void
    {
        static::addGlobalScope('tenant', function (Builder $builder) {
            if (app()->bound('currentTenantId')) {
                $builder->where($builder->getModel()->getTable() . '.tenant_id', app('currentTenantId'));
            }
        });

        static::creating(function ($model) {
            if (app()->bound('currentTenantId') && ! $model->tenant_id) {
                $model->tenant_id = app('currentTenantId');
            }
        });
    }
}
