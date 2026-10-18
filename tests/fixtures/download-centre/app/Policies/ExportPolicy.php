<?php

namespace App\Policies;

use App\Models\Export;
use App\Models\User;

class ExportPolicy
{
    public function view(User $user, Export $export): bool
    {
        return $user->id === $export->user_id;
    }

    public function delete(User $user, Export $export): bool
    {
        return $user->id === $export->user_id;
    }
}
